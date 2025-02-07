# email_notifier.py
import os
import logging
import smtplib
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
import pandas as pd
import aiosmtplib

logger = logging.getLogger(__name__)

class EmailNotifier:
    def __init__(self):
        load_dotenv()
        self.smtp_server = os.getenv('SMTP_SERVER', 'smtp.gmail.com')
        self.smtp_port = int(os.getenv('SMTP_PORT', '465'))
        self.smtp_username = os.getenv('SMTP_USERNAME')
        self.smtp_password = os.getenv('SMTP_PASSWORD')
        
        if not all([self.smtp_username, self.smtp_password]):
            raise ValueError("Missing email credentials in environment variables")
            
        self.user_preferences = {
            'ismetsemedov@gmail.com': ['analy','anali','python', 'sql', 'data', 'data science', 'ml engineer', 'machine learning', 'ai engineer'],
            'allahverdiyev.tural@hotmail.com': ['analy','anali','python', 'sql', 'data', 'data science', 'ml engineer', 'machine learning', 'ai engineer'],
            'qabil.isayev@icloud.com ':['fraud', 'risk', 'audit', 'control', 'nəzarət', 'compliance','frod'],
            'mammadova.arzu@outlook.com ':['fraud', 'risk', 'audit', 'control', 'nəzarət', 'compliance','frod'],
        }
        

    def _create_email_content(self, matching_jobs: pd.DataFrame) -> str:
        html_content = """
        <html>
            <head>
                <style>
                    table { border-collapse: collapse; width: 100%; }
                    th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
                    th { background-color: #f2f2f2; }
                    tr:nth-child(even) { background-color: #f9f9f9; }
                    .job-link { color: #0066cc; text-decoration: none; }
                </style>
            </head>
            <body>
                <h2>New Job Matches Found</h2>
                <table>
                    <tr>
                        <th>Company</th>
                        <th>Position</th>
                        <th>Apply Link</th>
                    </tr>
        """
        
        for _, job in matching_jobs.iterrows():
            html_content += f"""
                <tr>
                    <td>{job['company']}</td>
                    <td>{job['vacancy']}</td>
                    <td><a href="{job['apply_link']}" class="job-link">Apply Now</a></td>
                </tr>
            """
            
        html_content += """
                </table>
            </body>
        </html>
        """
        return html_content

    async def send_notification(self, jobs_df: pd.DataFrame):
        if jobs_df.empty:
            logger.warning("No jobs to send notifications for")
            return

        for user_email, keywords in self.user_preferences.items():
            try:
                pattern = '|'.join(map(re.escape, keywords))
                matching_jobs = jobs_df[
                    jobs_df['vacancy'].str.contains(pattern, case=False, na=False, regex=True)
                ]
                
                if matching_jobs.empty:
                    continue

                msg = MIMEMultipart('alternative')
                msg['Subject'] = f'New Job Matches Found - {len(matching_jobs)} positions'
                msg['From'] = self.smtp_username
                msg['To'] = user_email
                
                html_content = self._create_email_content(matching_jobs)
                msg.attach(MIMEText(html_content, 'html'))

                try:
                    async with aiosmtplib.SMTP(hostname=self.smtp_server, port=self.smtp_port) as smtp:
                        # await smtp.starttls()
                        await smtp.login(self.smtp_username, self.smtp_password)
                        await smtp.send_message(msg)
                        logger.info(f"Email notification sent to {user_email}")

                except aiosmtplib.errors.SMTPException as e:
                    logger.error(f"SMTP error sending email to {user_email}: {str(e)}")

            except Exception as e:
                logger.error(f"Error processing notifications for {user_email}: {str(e)}")