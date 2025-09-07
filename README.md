# bot-Q-A

A Telegram bot for creating and taking tests with support for text questions, multiple choice questions, and photo questions.

## Features

- **User Roles**: Separate interfaces for teachers and students
- **Test Creation**: Teachers can create tests with various question types:
  - Text questions with multiple choice answers
  - Text questions with text input answers
  - Photo questions with multiple choice answers
  - Photo questions with text input answers
- **Test Taking**: Students can take tests and receive immediate feedback
- **Results Tracking**: Teachers can view detailed results and statistics
- **Session Management**: Persistent test sessions using SQLite database
- **QR Codes**: Generate QR codes for easy test access

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/Evgeiy23/bot-Q-A
   cd bot-Q/A
   ```

2. Install dependencies:
   ```bash
   pip install aiogram==3.7.0 qrcode[pil] Pillow
   ```

3. Set up your bot token:
   - Create a bot with [@BotFather](https://t.me/BotFather) on Telegram
   - Replace `BOT_TOKEN` in [main.py] with your bot's token

## Usage

1. Run the bot:
   ```bash
   python main.py
   ```

2. Interact with the bot:
   - Start a conversation with your bot on Telegram
   - Select your role (Teacher or Student)
   - Teachers can create tests and view results
   - Students can take tests using links or QR codes

## Question Types

### Text Questions
- **Multiple Choice**: Students select from predefined options
- **Text Input**: Students type their answers

### Photo Questions
- **Photo with Multiple Choice**: Students select from predefined options after viewing a photo
- **Photo with Text Input**: Students type their answers after viewing a photo

## Database

The bot uses SQLite for session persistence:
- `user_test_sessions`: Stores active test sessions
- `active_user_tests`: Tracks which test each user is currently taking

## Project Structure

```
bot Q/A/
├── main.py              # Main bot implementation
├── bot_sessions.db      # SQLite database (created automatically)
├── requirements.txt     # Python dependencies
└── README.md           # This file
```

## Dependencies

- Python 3.7+
- aiogram 3.7.0
- qrcode 7.4.2
- Pillow 10.0.0

