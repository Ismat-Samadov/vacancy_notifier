---

# Job Scraper and Email Notifier

This Python script is designed to scrape job listings from various company websites and send email notifications to specific recipients based on keywords found in the job listings. It provides a flexible way to monitor job opportunities that match your criteria.

## Table of Contents

- [Features](#features)
- [Getting Started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Installation](#installation)
- [Usage](#usage)
- [Configuration](#configuration)
  - [Email Configuration](#email-configuration)
  - [Recipient Configuration](#recipient-configuration)
- [Customization](#customization)
- [Contributing](#contributing)
- [License](#license)

## Features

- Scrapes job listings from multiple company websites.
- Filters job listings based on specific keywords (e.g., "data," "audit," "scrum").
- Sends email notifications to different recipients for each keyword.
- Uses Pandas for data management and email.mime for email creation.

## Getting Started

### Prerequisites

Before running the script, make sure you have the following prerequisites installed:

- Python 3.7 or higher
- Required Python packages (install them using `pip`):
  - `beautifulsoup4` (4.12.2)
  - `pandas` (2.1.1)
  - `requests` (2.31.0)
  - `urllib3` (1.26.16)
  - `brotli` (1.1.0)

### Installation

1. Clone the repository to your local machine:

   ```bash
   git clone https://github.com/Ismat-Samadov/vacancy_notifier.git
   cd vacancy_notifier
   ```

2. Create a virtual environment (recommended) and activate it:

   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows, use: venv\Scripts\activate
   ```

3. Install the required Python packages from the `requirements.txt` file:

   ```bash
   pip install -r requirements.txt
   ```

## Usage

To use the job scraper and email notifier, follow these steps:

1. Edit the script to customize it for your specific needs.

2. Set up your email configuration and recipient configuration (see [Configuration](#configuration)).

3. Run the script:

   ```bash
   python main.py
   ```

The script will scrape job listings, filter them based on your criteria, and send email notifications to the specified recipients.

## Configuration

### Email Configuration

In the script, you'll need to configure your email settings to send notifications. This includes the sender's email address, email password, and SMTP server settings.

```python
from_email = "your_email@gmail.com"
email_password = "your_email_password"
```

Make sure to secure your email credentials and consider using environment variables for enhanced security.

### Recipient Configuration

In the `filter_and_send_emails` method, you can specify recipients for different job categories. Customize this section to match your requirements.

```python
if not df_data.empty:
    self.send_email(df_data, "recipient1@email.com")
```

## Customization

You can customize the script to scrape job listings from additional company websites or modify the keywords used for filtering. Additionally, you can extend or modify the email notification logic to match your preferences.

## Contributing

Contributions are welcome! If you have ideas for improvements or new features, please open an issue or submit a pull request.


---
