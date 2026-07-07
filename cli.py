#!/usr/bin/env python3
import os
import sys
import json
import asyncio
import argparse
import uuid

# Add the project root to sys.path to enable importing the 'schemapilot' package
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.append(project_root)

# Load env variables
from dotenv import load_dotenv
load_dotenv(os.path.join(project_root, ".env"))

from schemapilot.db import get_db
from schemapilot.agent import SchemaPilotAgent
from schemapilot.llm import get_model_manager

# SOTA Terminal visual imports
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

# SOTA Prompt toolkit imports
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.formatted_text import HTML

class SchemaPilotCLI:
    def __init__(self, llm_config=None):
        self.history = []
        self.llm_config = llm_config or {}
        self.db = get_db()
        self.model_manager = get_model_manager()
        self.console = Console()
        
        # Setup REPL history session in user's home configuration directory
        USER_CONFIG_DIR = os.path.expanduser("~/.config/schemapilot")
        history_file = os.path.join(USER_CONFIG_DIR, ".repl_history")
        os.makedirs(os.path.dirname(history_file), exist_ok=True)
        self.session = PromptSession(history=FileHistory(history_file))

    def format_agent_name(self, agent):
        colors = {
            "Architect": "bold blue",
            "Programmer": "bold green",
            "Sentry": "bold yellow",
            "Executor": "bold magenta",
            "Analyst": "bold cyan",
        }
        color = colors.get(agent, "white")
        return f"[{color}][{agent}][/{color}]"

    def handle_event(self, event_data):
        """Formats and displays execution progress in the terminal using Rich."""
        event_type = event_data.get("event")
        
        if event_type == "agent_message":
            # Direct logging if not wrapped in status status
            agent = event_data.get("agent", "Agent")
            msg = event_data.get("message", "")
            self.console.print(f"\n{self.format_agent_name(agent)} {msg}")
            
        elif event_type == "final_output":
            text = event_data.get("text", "")
            self.console.print("\n[bold green]✨ SchemaPilot:[/bold green]")
            self.console.print(Markdown(text))
            
        elif event_type == "swarm_completed":
            sql = event_data.get("sql", "")
            summary = event_data.get("summary", "")
            raw_data = event_data.get("raw_data", {})
            
            # Print SQL Panel
            self.console.print()
            self.console.print(Panel(
                Syntax(sql, "sql", theme="monokai", line_numbers=True, word_wrap=True),
                title="💻 Executed SQL Query",
                border_style="blue",
                expand=False
            ))
            
            # Print Raw Data Table
            if isinstance(raw_data, dict) and "rows" in raw_data and raw_data["rows"]:
                cols = raw_data.get("columns", [])
                rows = raw_data.get("rows", [])
                
                table = Table(show_header=True, header_style="bold cyan", border_style="dim")
                for col in cols:
                    table.add_column(col)
                
                # Show top 15 records
                for row in rows[:15]:
                    table.add_row(*[str(row.get(col) if row.get(col) is not None else "") for col in cols])
                    
                self.console.print("\n[bold cyan]📋 Results Preview:[/bold cyan]")
                self.console.print(table)
                if len(rows) > 15:
                    self.console.print(f"[dim]* Truncated display preview to 15 of {len(rows)} total rows. *[/dim]")

            # Print Summary MD
            self.console.print("\n[bold green]📊 Analysis Summary:[/bold green]")
            self.console.print(Markdown(summary))
            self.console.print()
            
            self.history.append({"role": "assistant", "content": summary})
            
        elif event_type == "error":
            err = event_data.get("error", "")
            self.console.print(f"\n[bold red]❌ Error:[/bold red] {err}", style="red")

    async def execute_query(self, query):
        """Executes query locally with a dynamic status spinner."""
        if not self.db.active_id:
            self.console.print("[bold red]❌ Error: No active database connection configured.[/bold red]")
            self.console.print("Configure a database connection using: [bold]schemapilot --add-conn[/bold]")
            return

        agent = SchemaPilotAgent()
        self.history.append({"role": "user", "content": query})
        
        try:
            # SOTA Status Spinner wraps the execution loop
            with self.console.status("[bold blue]Resolving database schema...") as status:
                async for event_str in agent.execute(query, self.history[:-1], self.llm_config):
                    if event_str.strip():
                        try:
                            event_data = json.loads(event_str.strip())
                            
                            # Intercept agent steps to update status text dynamically
                            if event_data.get("event") == "agent_message":
                                agent_name = event_data.get("agent", "Agent")
                                msg = event_data.get("message", "").split("\n")[0]
                                status.update(f"[bold blue][{agent_name}] {msg}...")
                            else:
                                self.handle_event(event_data)
                        except json.JSONDecodeError:
                            pass
        except Exception as e:
            self.console.print(f"[bold red]❌ Local Query Execution Error: {e}[/bold red]")

    def interactive_shell(self):
        # Fetch active connection details
        active_db = "None"
        for conn in self.db.get_connections_list():
            if conn["is_active"]:
                active_db = f"{conn['name']} ({conn['db_type'].upper()})"
                
        # Fetch active model profile details
        active_model = "Default (.env)"
        active_profile = self.model_manager.get_active_profile()
        if self.model_manager.active_id:
            active_model = f"{self.model_manager.active_id} ({active_profile.get('model_name')})"
                
        self.console.print(Panel(
            Text.assemble(
                ("SchemaPilot Console CLI (Local Only)\n", "bold blue"),
                ("Database Connection: ", "bold"), (f"{active_db}\n", "green"),
                ("AI Model Profile:   ", "bold"), (f"{active_model}", "green")
            ),
            border_style="blue",
            expand=False
        ))
        
        self.console.print("Type your database query. Press [bold]Up/Down[/bold] for history. Type '[bold]exit[/bold]' to quit.")
        
        while True:
            try:
                # SOTA Prompt prompt_toolkit session
                query = self.session.prompt(HTML("<cyan><b>🤔 Ask SchemaPilot &gt; </b></cyan>")).strip()
                if not query:
                    continue
                if query.lower() in ["exit", "quit"]:
                    self.console.print("\nGoodbye! 👋")
                    break
                asyncio.run(self.execute_query(query))
            except KeyboardInterrupt:
                self.console.print("\nGoodbye! 👋")
                break
            except Exception as e:
                self.console.print(f"[bold red]❌ Error: {e}[/bold red]")

    # ----------------------------------------------------
    # Database Connection Profile Management
    # ----------------------------------------------------

    def add_connection_prompt(self):
        """Interactive console questionnaire to link a new database."""
        self.console.print("\n[bold blue]=== Add Database Connection ===[/bold blue]")
        name = input("Enter a friendly profile name (e.g. Sales DB): ").strip()
        if not name:
            print("❌ Connection name is required.")
            return

        db_type = input("Enter database dialect (postgres, mysql, sqlite): ").strip().lower()
        if db_type not in ["postgres", "mysql", "sqlite"]:
            print("❌ Invalid dialect. Only postgres, mysql, and sqlite are supported.")
            return

        config = {"name": name, "db_type": db_type}
        
        if db_type != "sqlite":
            config["host"] = input("Enter database host (default: localhost): ").strip() or "localhost"
            port_input = input(f"Enter port (default: {'5432' if db_type == 'postgres' else '3306'}): ").strip()
            config["port"] = int(port_input) if port_input else (5432 if db_type == "postgres" else 3306)
            config["username"] = input("Enter database username: ").strip()
            config["password"] = input("Enter database password: ").strip()
            config["database"] = input("Enter target database name: ").strip()
        else:
            config["database"] = input("Enter SQLite database file path: ").strip()

        self.console.print("\n⏳ Testing connection parameters...")
        success, message = self.db.test_connection(config)
        if success:
            conn_id = str(uuid.uuid4())
            self.db.add_connection(conn_id, config)
            if not self.db.active_id:
                self.db.select_connection(conn_id)
            self.console.print(f"[bold green]✅ Connection saved and verified successfully! Profile ID: {conn_id}[/bold green]")
        else:
            self.console.print(f"[bold red]❌ Connection test failed: {message}[/bold red]")

    def list_connections(self):
        """Outputs saved connections registry to console."""
        conns = self.db.get_connections_list()
        self.console.print("\n[bold blue]=== Configured Database Connections ===[/bold blue]")
        if not conns:
            print("No saved connection profiles found. Use --add-conn to configure one.")
            return
            
        table = Table(show_header=True, header_style="bold blue")
        table.add_column("Status")
        table.add_column("Connection ID")
        table.add_column("Friendly Name")
        table.add_column("Dialect")
        table.add_column("Host / Path")
        
        for conn in conns:
            status = "[bold green]ACTIVE[/bold green]" if conn["is_active"] else ""
            host_str = f"{conn['host']}:{conn['port']} / {conn['database']}" if conn["db_type"] != "sqlite" else conn["database"]
            table.add_row(status, conn["id"], conn["name"], conn["db_type"].upper(), host_str)
            
        self.console.print(table)

    def select_connection(self, conn_id):
        """Activates target connection profile."""
        try:
            success = self.db.select_connection(conn_id)
            if success:
                for cid in self.db.connections:
                    self.db.connections[cid]["is_active"] = (cid == conn_id)
                self.db.save_connections()
                self.console.print(f"[bold green]✅ Switched active database connection profile to: {conn_id}[/bold green]")
            else:
                self.console.print(f"[bold red]❌ Connection profile ID '{conn_id}' not found.[/bold red]", style="red")
        except Exception as e:
            self.console.print(f"[bold red]❌ Connection switch failed: {e}[/bold red]", style="red")

    def delete_connection(self, conn_id):
        """Removes profile from connections dictionary."""
        if conn_id in self.db.connections:
            self.db.delete_connection(conn_id)
            self.console.print(f"[bold green]✅ Connection profile '{conn_id}' deleted successfully.[/bold green]")
        else:
            self.console.print(f"[bold red]❌ Connection profile ID '{conn_id}' not found.[/bold red]", style="red")

    # ----------------------------------------------------
    # AI Model Profile Management
    # ----------------------------------------------------

    def add_model_prompt(self):
        """Interactive console questionnaire to link a new model configuration."""
        self.console.print("\n[bold blue]=== Add AI Model Profile ===[/bold blue]")
        profile_id = input("Enter a profile identifier name (e.g. gemini-flash): ").strip()
        if not profile_id:
            print("❌ Profile identifier is required.")
            return

        provider = input("Select provider (google, openai, anthropic, local): ").strip().lower()
        if provider not in ["google", "openai", "anthropic", "local"]:
            print("❌ Invalid provider. Select google, openai, anthropic, or local.")
            return

        model_name = input("Enter model name (e.g. gemini-2.0-flash, gpt-4o, llama3): ").strip()
        if not model_name:
            print("❌ Model name is required.")
            return

        config = {
            "provider": provider,
            "model_name": model_name,
            "api_key": "",
            "base_url": ""
        }

        if provider != "local":
            config["api_key"] = input("Enter API authentication key: ").strip()
        else:
            config["base_url"] = input("Enter local server base URL (e.g. http://localhost:11434/v1): ").strip()
            config["api_key"] = input("Enter API key (optional for local): ").strip()

        self.model_manager.add_profile(profile_id, config)
        if not self.model_manager.active_id:
            self.model_manager.select_profile(profile_id)
            
        self.console.print(f"[bold green]✅ AI Model profile '{profile_id}' saved successfully![/bold green]")

    def list_models(self):
        """Outputs saved models registry to console, masking keys."""
        profiles = self.model_manager.get_profiles_list()
        self.console.print("\n[bold blue]=== Configured AI Model Profiles ===[/bold blue]")
        if not profiles:
            print("No custom LLM profiles found. Falling back to default .env configuration.")
            return

        table = Table(show_header=True, header_style="bold blue")
        table.add_column("Status")
        table.add_column("Profile ID")
        table.add_column("Provider")
        table.add_column("Model Name")
        table.add_column("Key / Endpoint")
        
        for prof in profiles:
            status = "[bold green]ACTIVE[/bold green]" if prof["is_active"] else ""
            if prof["provider"] != "local":
                key = prof["api_key"]
                masked = key[:4] + "..." + key[-4:] if len(key) > 8 else "..."
            else:
                masked = prof["base_url"]
            table.add_row(status, prof["id"], prof["provider"].upper(), prof["model_name"], masked)
            
        self.console.print(table)

    def select_model(self, profile_id):
        """Sets the active model profile."""
        if self.model_manager.select_profile(profile_id):
            self.console.print(f"[bold green]✅ Switched active AI Model profile to: {profile_id}[/bold green]")
        else:
            self.console.print(f"[bold red]❌ AI Model profile ID '{profile_id}' not found.[/bold red]", style="red")

    def delete_model(self, profile_id):
        """Deletes a saved model profile."""
        if profile_id in self.model_manager.profiles:
            self.model_manager.delete_profile(profile_id)
            self.console.print(f"[bold green]✅ AI Model profile '{profile_id}' deleted successfully.[/bold green]")
        else:
            self.console.print(f"[bold red]❌ AI Model profile ID '{profile_id}' not found.[/bold red]", style="red")


def main():
    parser = argparse.ArgumentParser(description="SchemaPilot Terminal CLI Database Client (Local-only)")
    parser.add_argument("query", nargs="?", help="A single query to execute against the database")
    
    # Connection Management CLI flags
    parser.add_argument("--add-conn", action="store_true", help="Interactively add and test a new database profile link")
    parser.add_argument("--list-conns", action="store_true", help="List all saved database connection profiles")
    parser.add_argument("--select-conn", metavar="CONN_ID", help="Set the active database profile by its ID")
    parser.add_argument("--delete-conn", metavar="CONN_ID", help="Delete a database connection profile by its ID")

    # LLM Profile Management CLI flags
    parser.add_argument("--add-model", action="store_true", help="Interactively add a new AI model profile configuration")
    parser.add_argument("--list-models", action="store_true", help="List all saved AI model configurations")
    parser.add_argument("--select-model", metavar="MODEL_ID", help="Set the active AI model profile by its ID")
    parser.add_argument("--delete-model", metavar="MODEL_ID", help="Delete an AI model profile configuration by its ID")

    # Persistent dynamic save option
    parser.add_argument("--save-llm", action="store_true", help="Persist the model settings passed as command-line arguments to the default configuration profile")

    # Quick Overrides
    parser.add_argument("--provider", help="AI provider override ('google', 'openai', 'anthropic', 'local')")
    parser.add_argument("--model", help="AI model override (e.g. 'gemini-2.0-flash', 'gpt-4o')")
    parser.add_argument("--api-key", help="API authentication key override")
    parser.add_argument("--base-url", help="Local server endpoint base URL override")

    args = parser.parse_args()
    
    # LLM Config overrides
    llm_config = {}
    if args.provider: llm_config["provider"] = args.provider
    if args.model: llm_config["model_name"] = args.model
    if args.api_key: llm_config["api_key"] = args.api_key
    if args.base_url: llm_config["base_url"] = args.base_url
    
    cli = SchemaPilotCLI(llm_config=llm_config)
    
    # Evaluate connection and config management actions
    if args.save_llm:
        if not llm_config:
            print("\033[91m❌ Error: Specify at least one LLM parameter (--provider, --model, --api-key, or --base-url) to save.\033[0m", file=sys.stderr)
            return
        from schemapilot.llm import save_llm_config, load_saved_llm_config
        existing = load_saved_llm_config()
        merged = {**existing, **llm_config}
        save_llm_config(merged)
        print("\033[92m✅ Saved LLM settings successfully as default configuration profile!\033[0m")
        return

    if args.add_conn:
        cli.add_connection_prompt()
    elif args.list_conns:
        cli.list_connections()
    elif args.select_conn:
        cli.select_connection(args.select_conn)
    elif args.delete_conn:
        cli.delete_connection(args.delete_conn)
        
    # Evaluate model configuration actions
    elif args.add_model:
        cli.add_model_prompt()
    elif args.list_models:
        cli.list_models()
    elif args.select_model:
        cli.select_model(args.select_model)
    elif args.delete_model:
        cli.delete_model(args.delete_model)
        
    # Evaluate query execution
    elif args.query:
        asyncio.run(cli.execute_query(args.query))
    else:
        # If no flags are passed, launch REPL
        has_action = any([args.add_conn, args.list_conns, args.select_conn, args.delete_conn,
                          args.add_model, args.list_models, args.select_model, args.delete_model,
                          args.save_llm])
        if not has_action:
            cli.interactive_shell()

if __name__ == "__main__":
    main()