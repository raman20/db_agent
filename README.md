# Oracle: Your Intelligent Database Assistant

Oracle is a powerful and friendly database assistant that helps you interact with your MySQL database through natural language. It understands your questions, translates them into SQL queries, and presents the results in a clear, readable format.

## Features

- 🤖 Natural language interface for database queries
- 📊 Interactive command-line interface
- 🔍 Intelligent intent detection
- 💡 Smart response formatting
- 🛠️ Support for all MySQL operations
- 🎯 Context-aware responses

## Installation

1. Clone the repository:
```bash
git clone https://github.com/yourusername/oracle.git
cd oracle
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Set up environment variables:
```bash
cp .env.example .env
# Edit .env with your database credentials
```

## Usage

### Interactive Mode

Start the interactive CLI:
```bash
python main.py
```

Example interactions:
```
🌟 Welcome to Oracle, your intelligent database assistant!
I'm here to help you explore and manage your database.
Type 'help' to see what I can do, or ask me anything about your data.

🤔 Your question: help

📚 I can help you with your database in several ways:

1. Basic Commands:
   - tables: List all tables in the database
   - schema <table>: Show schema of a specific table
   - stats: Show database statistics

2. Ask Questions About Your Data:
   - 'Show me all customers'
   - 'How many orders do we have?'
   - 'What's the average order value?'
   - 'Find customers who haven't ordered in 30 days'

3. Data Analysis:
   - 'Show me sales trends by month'
   - 'What are our top selling products?'
   - 'Which customers have the highest lifetime value?'

4. Data Management:
   - 'Add a new customer'
   - 'Update customer information'
   - 'Delete inactive records'

🤖 I can also understand greetings and farewells!

💡 Just ask me anything about your database, and I'll do my best to help!
```

### Command-Line Mode

Run a single query:
```bash
python main.py "show me all customers"
```

## Project Structure

```
oracle/
├── main.py           # Entry point of the application
├── cli.py            # Command-line interface
├── database.py       # Database connection and operations
├── model.py          # Language model configuration
├── agent.py          # Agent creation and management
├── config.py         # Configuration and constants
├── requirements.txt  # Project dependencies
└── .env              # Environment variables
```

## Available Commands

- `tables`: List all tables in the database
- `schema <table>`: Show schema of a specific table
- `stats`: Show database statistics
- `help`: Show help information
- `exit`: Quit the program

## Environment Variables

Create a `.env` file with the following variables:
```
DB_USERNAME=your_username
DB_PASSWORD=your_password
DB_HOST=localhost
DB_PORT=3306
DB_NAME=your_database
GOOGLE_API_KEY=your_api_key
```

## Requirements

- Python 3.8+
- MySQL 5.7+
- Google API key for Gemini model

## Contributing

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add some amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- Built with Python and LangChain
- Powered by Google's Gemini model
- Inspired by the need for better database interaction 