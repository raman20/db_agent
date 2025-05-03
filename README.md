# MySQL Agent: The Oracle of Data

A powerful and elegant SQL agent that transforms complex database operations into simple conversations. This agent is capable of handling any MySQL operation with grace and precision.

## Features

- Natural language to SQL conversion
- Interactive command-line interface
- Support for all MySQL operations (SELECT, INSERT, UPDATE, DELETE, etc.)
- Error handling and retry mechanisms
- Database statistics and schema inspection
- Connection pooling and timeout management

## Installation

1. Clone the repository:
```bash
git clone https://github.com/yourusername/mysql-agent.git
cd mysql-agent
```

2. Create a virtual environment (recommended):
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

## Configuration

1. Create a `.env` file in the project root:
```bash
DB_USERNAME=your_db_username
DB_PASSWORD=your_db_password
DB_HOST=localhost
DB_PORT=3306
DB_NAME=your_database
GOOGLE_API_KEY=your_google_api_key
```

2. Update the database configuration in `mysql-agent.py` if needed.

## Usage

### Command Line Mode
```bash
python mysql-agent.py "your natural language query"
```

### Interactive Mode
```bash
python mysql-agent.py
```

Available commands in interactive mode:
- `help`: Show available commands
- `tables`: List all tables in the database
- `schema <table>`: Show schema of a specific table
- `stats`: Show database statistics
- `exit`: Quit the program

## Examples

```bash
# List all customers
python mysql-agent.py "Show me all customers"

# Add a new customer
python mysql-agent.py "Add a new customer named John Smith with phone 555-1234"

# Get database statistics
python mysql-agent.py "stats"
```

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- Built with [LangChain](https://www.langchain.com/)
- Powered by [Google's Gemini](https://ai.google.dev/)
- Database operations handled by [SQLAlchemy](https://www.sqlalchemy.org/) 