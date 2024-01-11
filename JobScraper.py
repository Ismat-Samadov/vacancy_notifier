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
        url = "https://www.azercell.com/az/about-us/career.html"
        response = requests.get(url)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            vacancies_section = soup.find("section", class_="section_vacancies")
            if vacancies_section:
                job_listings = vacancies_section.find_all("a", class_="vacancies__link")
                job_titles = []
                job_links = []
                for job in job_listings:
                    job_title = job.find("h4", class_="vacancies__name").text
                    job_link = job["href"]
                    job_titles.append(job_title)
                    job_links.append(job_link)
                df = pd.DataFrame({
                    'company': 'azercell',
                    "vacancy": job_titles,
                    "apply_link": job_links
                })
                return df
            else:
                print("Vacancies section not found on the page.")
        else:
            print("Failed to retrieve the page. Status code:", response.status_code)


    def scrape_pashabank(self):
        url = "https://careers.pashabank.az/az/page/vakansiyalar?q=data&branch="
        response = requests.get(url)
        vacancy_list = []
        apply_link_list = []
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            job_listings = soup.find_all('div', class_='what-we-do-item')
            for listing in job_listings:
                job_title = listing.find('h3').text
                apply_link = listing.find('a')['href']
                vacancy_list.append(job_title)
                apply_link_list.append(apply_link)
        else:
            print(f"Failed to retrieve the page. Status code: {response.status_code}")
        data = {
            'company': 'pashabank',
            'vacancy': vacancy_list,
            'apply_link': apply_link_list
        }
        df = pd.DataFrame(data)
        df = df.drop_duplicates(subset=['company', 'vacancy', 'apply_link'])
        return df


    def scrape_azerconnect(self):
        url = "https://www.azerconnect.az/careers"
        response = requests.get(url, verify=False)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            job_listings = soup.find_all('div', class_='CollapsibleItem_content__KGo_x')

            job_data = []
            apply_links = []
            for job in job_listings:
                job_details = job.text
                job_data.append(job_details)
                apply_link = job.find('a', class_='Button_button-blue__0wZ4l')['href']
                apply_links.append(apply_link)
            df = pd.DataFrame({'company': 'azerconnect',
                            'vacancy': job_data,
                            'apply_link': apply_links})
            return df

        else:
            print("Failed to retrieve the web page.")
            return None


    def scrape_abb(self):
        base_url = "https://careers.abb-bank.az/api/vacancy/v2/get"
        job_vacancies = []
        page = 1

        while True:
            params = {"page": page}
            response = requests.get(base_url, params=params)

            if response.status_code == 200:
                data = response.json()["data"]

                if not data:
                    break

                for item in data:
                    title = item.get("title")
                    url = item.get("url")
                    job_vacancies.append({"company": "abb", "vacancy": title, "apply_link": url})
                page += 1
            else:
                print(f"Failed to retrieve data for page {page}. Status code: {response.status_code}")
                break

        df = pd.DataFrame(job_vacancies)
        return df

    def scrape_rabitebank(self):
            url = "https://www.rabitabank.com/insan-resurslari/vakansiyalar"
            response = requests.get(url)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                job_titles = []
                job_links = []
                for element in soup.select('#vacancies > div > a'):
                    job_titles.append(element.text.strip())
                    job_links.append(element['href'])
                data = {
                    'company': 'rabitebank',
                    'vacancy': job_titles,
                    'apply_link': job_links,
                }
                df = pd.DataFrame(data)

                return df

            else:
                print("Failed to retrieve the page. Status code:", response.status_code)
                return None

    def send_email(self, data_frame, to_email):
        html_table = data_frame.to_html(index=False)
        from_email = "ismetsemedov@gmail.com"
        email_password = "lmjareknmmweotsp"
        subject = "🔥🔥🔥 J O B S 🔥🔥🔥"
        message = MIMEMultipart()
        message["From"] = from_email
        message["To"] = to_email
        message["Subject"] = subject
        message.attach(MIMEText(html_table, "html"))
        try:
            server = smtplib.SMTP("smtp.gmail.com", 587)
            server.starttls()
            server.login(from_email, email_password)
            server.sendmail(from_email, to_email, message.as_string())
            server.quit()
            print("Email sent successfully.")
        except Exception as e:
            print("Email could not be sent. Error:", str(e))

    def get_data(self):
        abb_df = self.scrape_abb()
        azerconnect_df = self.scrape_azerconnect()
        pashabank_df = self.scrape_pashabank()
        azercell_df = self.scrape_azercell()
        rabitebank_df = self.scrape_rabitebank()

        self.data = pd.concat([pashabank_df, azerconnect_df, azercell_df, abb_df, rabitebank_df], ignore_index=True)

    def filter_and_send_emails(self):
        ismat = "ismetsemedli@mail.ru"
        kamal = "kamalkhalilov7@gmail.com"
        azar  = "azer14480@gmail.com"
        nigar = "nigar.ly77@gmail.com"
        rustam = "rustam.isgandarli@outlook.com"
        if self.data is not None:
            df_data = self.data[self.data['vacancy'].str.contains("data", case=False)].reset_index(drop=True)
            df_audit = self.data[self.data['vacancy'].str.contains("audit", case=False)].reset_index(drop=True)
            df_scrum = self.data[self.data['vacancy'].str.contains("scrum", case=False)].reset_index(drop=True)
            df_business = self.data[self.data['vacancy'].str.contains("biznes", case=False)].reset_index(drop=True)
            df_fraud = self.data[self.data['vacancy'].str.contains("fraud", case=False)].reset_index(drop=True)
            if not df_data.empty:
                self.send_email(df_data,ismat )
                self.send_email(df_data,kamal )
            if not df_business.empty:
                self.send_email(df_business,kamal)
            if not df_scrum.empty:
                self.send_email(df_scrum,azar)
            if not df_audit.empty:
                self.send_email(df_audit, nigar)
                self.send_email(df_audit, rustam)
            if not df_fraud.empty:
                self.send_email(df_fraud, nigar)
                self.send_email(df_fraud, rustam)

if __name__ == "__main__":
    job_scraper = JobScraper()
    job_scraper.get_data()
    job_scraper.filter_and_send_emails()
