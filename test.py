# import bs4
# import requests
# import pandas as pd

# url = "https://www.rabitabank.com/insan-resurslari/vakansiyalar"
# response = requests.get(url)
# if response.status_code == 200:
#     soup = bs4.BeautifulSoup(response.text, 'html.parser')
#     job_titles = []
#     job_links = []
#     for element in soup.select('#vacancies > div > a'):
#         job_titles.append(element.text.strip())
#         job_links.append(element['href'])
#     data = {
#         'company':'rabitebank',
#         'vacancy': job_titles,
#         'apply_link': job_links,
#     }
#     df = pd.DataFrame(data)

#     print(df)

# else:
#     print("Failed to retrieve the page. Status code:", response.status_code)
import requests
import pandas as pd


# Example usage
rabitabank_job_data = rabitebank()
if rabitabank_job_data is not None:
    print(rabitabank_job_data)
