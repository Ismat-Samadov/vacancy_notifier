import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import pandas as pd
import requests
import urllib3
from bs4 import BeautifulSoup
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class JobScraper:
    def __init__(self):
        self.data = None

    def scrape_azercell(self):
        # Add your scraping code for azercell here
        pass

    def scrape_pashabank(self):
        # Add your scraping code for pashabank here
        pass

    def scrape_azerconnect(self):
        # Add your scraping code for azerconnect here
        pass

    def scrape_abb(self):
        # Add your scraping code for abb here
        pass

    def scrape_rabitebank(self):
        # Add your scraping code for rabitebank here
        pass

    def send_email(self, data_frame, to_email):
        # Add your email sending code here
        pass

    def get_data(self):
        abb_df = self.scrape_abb()
        azerconnect_df = self.scrape_azerconnect()
        pashabank_df = self.scrape_pashabank()
        azercell_df = self.scrape_azercell()
        rabitebank_df = self.scrape_rabitebank()

        self.data = pd.concat([pashabank_df, azerconnect_df, azercell_df, abb_df, rabitebank_df], ignore_index=True)

    def filter_and_send_emails(self):
        if self.data is not None:
            df_data = self.data[self.data['vacancy'].str.contains("data", case=False)].reset_index(drop=True)
            df_audit = self.data[self.data['vacancy'].str.contains("audit", case=False)].reset_index(drop=True)

            if not df_data.empty:
                self.send_email(df_data, "ismetsemedli@mail.ru")
            if not df_audit.empty:
                self.send_email(df_audit, "nigar.ly77@gmail.com")

if __name__ == "__main__":
    job_scraper = JobScraper()
    job_scraper.get_data()
    job_scraper.filter_and_send_emails()
