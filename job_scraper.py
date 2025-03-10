# job_scraper.py - Asynchronous web scraper for job listings
import logging
import aiohttp
import asyncio
import json
import re
import random
import pandas as pd
from bs4 import BeautifulSoup
from urllib.parse import urljoin, quote
import functools
from typing import Optional, Dict, List
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.ERROR,
    format='%(asctime)s %(levelname)s:%(name)s:%(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

class RateLimiter:
    def __init__(self, calls_per_second=2):
        self.calls_per_second = calls_per_second
        self.last_call = datetime.min
        self._lock = asyncio.Lock()
    
    async def acquire(self):
        async with self._lock:
            now = datetime.now()
            elapsed = (now - self.last_call).total_seconds()
            if elapsed < 1/self.calls_per_second:
                await asyncio.sleep(1/self.calls_per_second - elapsed)
            self.last_call = datetime.now()

class CustomLogger:
    def __init__(self, name):
        self.logger = logging.getLogger(name)
        self._last_message = None
        self._repeat_count = 0
        
    def info(self, message):
        if message == self._last_message:
            self._repeat_count += 1
            return
        if self._repeat_count > 0:
            self.logger.info(f"Last message repeated {self._repeat_count} times")
        self._last_message = message
        self._repeat_count = 0
        self.logger.info(message)
       
class RetryableError(Exception):
    """Raised when an error is temporary and the operation should be retried."""
    pass

class NonRetryableError(Exception):
    """Raised when an error is permanent and retrying would not help."""
    pass

def scraper_error_handler(func):
    """Decorator to handle scraper errors consistently."""
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Error in {func.__name__}: {str(e)}", exc_info=True)
            return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])
    return wrapper

class JobScraper:
    def __init__(self):
        self.rate_limiter = RateLimiter(calls_per_second=2)
        self.data = None
        self.session_config = {
            'timeout': aiohttp.ClientTimeout(total=60, connect=20, sock_read=30),
            'headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9',
                'Accept-Language': 'en-US,en;q=0.5'
            }
        }
        
    async def save_to_db(self, df: pd.DataFrame) -> None:
        """Save scraped jobs to database/CSV asynchronously"""
        if df is None or df.empty:
            logger.warning("No data to save")
            return
                
        try:
            # Add timestamp
            df['scrape_date'] = datetime.now()
                
            # Remove duplicates
            df = df.drop_duplicates(subset=['company', 'vacancy', 'apply_link'])
                
            # Save to CSV using aiofiles
            import aiofiles
            csv_data = df.to_csv(index=False)
            async with aiofiles.open('jobs.csv', mode='w') as f:
                await f.write(csv_data)
            logger.info(f"Saved {len(df)} jobs to CSV")
                    
        except Exception as e:
            logger.error(f"Error saving data: {str(e)}")

    async def fetch_url_async(self, url: str, session: aiohttp.ClientSession, params=None, headers=None, verify_ssl=True) -> Optional[str]:
        """Enhanced fetch with better error handling and encoding detection"""
        retries = 3
        delay = 1
        
        for attempt in range(retries):
            try:
                if headers is None:
                    headers = self.session_config['headers']
                
                async with session.get(url, params=params, headers=headers, ssl=verify_ssl) as response:
                    if response.status == 403:
                        logger.warning(f"Access forbidden (403) for {url}, retrying with different headers")
                        headers = {
                            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15',
                            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9',
                            'Accept-Language': 'en-US,en;q=0.9',
                            'Cache-Control': 'no-cache',
                            'Pragma': 'no-cache'
                        }
                        continue
                        
                    if response.status != 200:
                        logger.error(f"HTTP {response.status} error for {url}")
                        await asyncio.sleep(delay * (attempt + 1))
                        continue
                        
                    try:
                        return await response.text()
                    except UnicodeDecodeError:
                        content = await response.read()
                        for encoding in ['utf-8', 'latin1', 'cp1252', 'iso-8859-1']:
                            try:
                                return content.decode(encoding)
                            except UnicodeDecodeError:
                                continue
                                
                        logger.error(f"Failed to decode response from {url}")
                        return None
                        
            except aiohttp.ClientError as e:
                logger.error(f"Connection error for {url}: {str(e)}")
                await asyncio.sleep(delay * (attempt + 1))
                continue
                
            except asyncio.TimeoutError:
                logger.error(f"Timeout error for {url}")
                await asyncio.sleep(delay * (attempt + 1))
                continue
                
        return None

    async def parse_json_response(self, response: str, default=None) -> dict:
        """Parse JSON response with improved error handling"""
        if not response:
            return default or {}
        try:
            if isinstance(response, str):
                return json.loads(response)
            return response
        except json.JSONDecodeError as e:
            logger.error(f"JSON parsing error: {str(e)}")
            try:
                # Try to clean the response before parsing
                cleaned_response = response.strip()
                if cleaned_response.startswith("'") and cleaned_response.endswith("'"):
                    cleaned_response = cleaned_response[1:-1]
                return json.loads(cleaned_response)
            except json.JSONDecodeError:
                logger.error("Failed to parse JSON even after cleaning")
                return default or {}

    async def fetch_with_retry(self, url: str, session: aiohttp.ClientSession, 
                             max_retries: int = 3, **kwargs) -> Optional[str]:
        """
        Fetch URL content with exponential backoff retry logic
        """
        for attempt in range(max_retries):
            try:
                async with session.get(url, **kwargs) as response:
                    if response.status == 200:
                        return await response.text()
                    elif response.status in [429, 503]:
                        raise RetryableError(f"Rate limited: {response.status}")
                    else:
                        logger.error(f"Failed to fetch {url}: HTTP {response.status}")
                        return None
                        
            except (aiohttp.ClientError, RetryableError) as e:
                if attempt < max_retries - 1:
                    wait_time = (2 ** attempt) + random.uniform(0, 1)
                    logger.warning(f"Retry {attempt + 1} for {url} after {wait_time:.1f}s: {str(e)}")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"Max retries reached for {url}: {str(e)}")
                    return None
                    
            except Exception as e:
                logger.error(f"Unexpected error fetching {url}: {str(e)}")
                return None

    def _clean_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        try:
            # Remove duplicates
            df = df.drop_duplicates()
            
            # Clean string columns properly
            for col in ['company', 'vacancy', 'apply_link']:
                if col in df.columns:
                    df.loc[:, col] = df[col].astype(str).str.strip()
            
            # Drop rows with missing required values
            df = df.dropna(subset=['company', 'vacancy', 'apply_link'])
            
            # Validate URLs 
            df = df[df['apply_link'].str.startswith(('http://', 'https://'))]
            
            return df
            
        except Exception as e:
            logger.error(f"Error cleaning DataFrame: {str(e)}")
            return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

    
    async def get_data_async(self):
        """
        Main method to fetch and process job data asynchronously from multiple sources
        """
        timeout = aiohttp.ClientTimeout(total=300)  # 5 minutes total timeout
        connector = aiohttp.TCPConnector(
            limit=50,
            ttl_dns_cache=300,
            ssl=False
        )

        async with aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            raise_for_status=False,
            trust_env=True
        ) as session:
            try:
                # Define parser tasks
                parsers = {
                    'glorri': self.parse_glorri(session),
                    'azercell': self.parse_azercell(session),
                    'azerconnect': self.parse_azerconnect(session),
                    'djinni': self.parse_djinni_co(session),
                    'abb': self.parse_abb(session),
                    'hellojob': self.parse_hellojob_az(session),
                    'boss': self.parse_boss_az(session),
                    'ejob': self.parse_ejob_az(session),
                    'vakansiya': self.parse_vakansiya_az(session),
                    'ishelanlari': self.parse_ishelanlari_az(session),
                    'banker': self.parse_banker_az(session),
                    'smartjob': self.parse_smartjob_az(session),
                    'offer': self.parse_offer_az(session),
                    'isveren': self.parse_isveren_az(session),
                    'isqur': self.parse_isqur(session),
                    'kapitalbank': self.parse_kapitalbank(session),
                    'bankofbaku': self.parse_bank_of_baku_az(session),
                    'jobbox': self.parse_jobbox_az(session),
                    'vakansiyabiz': self.parse_vakansiya_biz(session),
                    'its': self.parse_its_gov(session),
                    'iselanlari': self.parse_is_elanlari_iilkin(session),
                    'tabib': self.parse_tabib_vacancies(session),
                    'projobs': self.parse_projobs_vacancies(session),
                    'azergold': self.parse_azergold(session),
                    'konsis': self.parse_konsis(session),
                    'bakuelectronics': self.parse_baku_electronics(session),
                    'asco': self.parse_asco(session),
                    'cbar': self.parse_cbar(session),
                    'ada': self.parse_ada(session),
                    'jobfinder': self.parse_jobfinder(session),
                    'regulator': self.scrape_regulator(session),
                    'ekaryera': self.scrape_ekaryera(session),
                    'bravo': self.scrape_bravosupermarket(session),
                    'mdm': self.scrape_mdm(session),
                    'arti': self.scrape_arti(session),
                    'staffy': self.scrape_staffy(session),
                    'position': self.scrape_position_az(session),
                    'hrin': self.scrape_hrin_co(session),
                    'un': self.scrape_un_jobs(session),
                    'oilfund': self.scrape_oilfund_jobs(session),
                    '1is': self.scrape_1is_az(session),
                    'themuse': self.scrape_themuse_api(session),
                    'dejobs': self.scrape_dejobs(session),
                    'hcb': self.scrape_hcb(session),
                    'bfb': self.scrape_bfb(session),
                    'airswift': self.scrape_airswift(session),
                    'orion': self.scrape_orion(session),
                    'hrcbaku': self.scrape_hrcbaku(session),
                    'jobsearch': self.parse_jobsearch_az(session),
                    'canscreen': self.scrape_canscreen(session),
                    'azercosmos': self.parse_azercosmos(session),
                }

                # Execute all parsers concurrently
                results = await asyncio.gather(*parsers.values(), return_exceptions=True)
                
                # Process results
                all_jobs = []
                for source, result in zip(parsers.keys(), results):
                    if isinstance(result, Exception):
                        logger.error(f"Error in {source} parser: {str(result)}")
                        await self.log_scraper_error(
                            scraper_method=source,
                            error_code=type(result).__name__,
                            error_message=str(result)
                        )
                        continue
                        
                    if isinstance(result, pd.DataFrame) and not result.empty:
                        result = self._clean_dataframe(result)
                        all_jobs.extend(result.to_dict('records'))
                        logger.info(f"Successfully scraped {len(result)} jobs from {source}")

                # Create final DataFrame
                if all_jobs:
                    self.data = pd.DataFrame(all_jobs)
                    self.data['scrape_date'] = datetime.now()
                    self.data.dropna(subset=['company', 'vacancy'], inplace=True)
                    
                    # Save to database
                    # self.save_to_db(self.data)
                    
                    logger.info(f"Successfully processed {len(self.data)} jobs total")
                else:
                    logger.warning("No jobs collected from any source")
                    self.data = pd.DataFrame(columns=['company', 'vacancy', 'apply_link', 'scrape_date'])

            except Exception as e:
                logger.error(f"Critical error in get_data_async: {str(e)}", exc_info=True)
                self.data = pd.DataFrame(columns=['company', 'vacancy', 'apply_link', 'scrape_date'])
                raise

            finally:
                if not session.closed:
                    await session.close()
            
    @scraper_error_handler
    async def parse_glorri(self, session):
        """
        Glorri job scraper with comprehensive error handling and modern headers
        """
        logger.info("Started scraping Glorri")
        
        # API endpoints
        companies_url = "https://atsapp.glorri.com/user-service-v2/companies/public"
        jobs_url = "https://atsapp.glorri.az/job-service-v2/jobs/company/{}/public"
        
        # Modern browser-like headers
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Origin': 'https://jobs.glorri.az',
            'Referer': 'https://jobs.glorri.az/',
            'Connection': 'keep-alive',
            'Sec-Ch-Ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-site',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache'
        }
        
        try:
            jobs = []
            offset = 0
            limit = 20
            max_retries = 3
            retry_delay = 2
            
            while True:
                # Fetch companies with pagination and retry logic
                companies_params = {
                    'limit': limit,
                    'offset': offset
                }
                
                companies_response = None
                for retry in range(max_retries):
                    try:
                        logger.info(f"Fetching companies with offset {offset} (Attempt {retry + 1}/{max_retries})")
                        companies_response = await self.fetch_url_async(
                            companies_url, 
                            session, 
                            params=companies_params,
                            headers=headers,
                            verify_ssl=False
                        )
                        
                        if companies_response:
                            break
                        
                        logger.warning(f"Attempt {retry + 1} failed, retrying in {retry_delay} seconds...")
                        await asyncio.sleep(retry_delay * (retry + 1))
                        
                    except Exception as e:
                        logger.error(f"Error during company fetch attempt {retry + 1}: {str(e)}")
                        if retry < max_retries - 1:
                            await asyncio.sleep(retry_delay * (retry + 1))
                        else:
                            logger.error("Max retries exceeded for company fetch")
                            break
                
                if not companies_response:
                    logger.error(f"Failed to fetch companies at offset {offset} after all retries")
                    break
                    
                try:
                    if isinstance(companies_response, str):
                        companies_response = json.loads(companies_response)
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse companies response at offset {offset}: {str(e)}")
                    break
                
                companies = companies_response.get('entities', [])
                if not companies:
                    break
                    
                total_companies = companies_response.get('totalCount', 0)
                logger.info(f"Processing {len(companies)} companies (Total: {total_companies})")
                
                # Process each company's jobs
                for company in companies:
                    company_name = company.get('name')
                    company_slug = company.get('slug')
                    job_count = company.get('jobCount', 0)
                    
                    if not company_slug:
                        continue
                        
                    logger.info(f"Fetching jobs for {company_name} (Expected: {job_count} jobs)")
                    
                    # Fetch jobs with pagination
                    job_skip = 0
                    job_limit = 18
                    
                    while True:
                        company_jobs_url = jobs_url.format(company_slug)
                        jobs_params = {
                            'skip': job_skip,
                            'limit': job_limit
                        }
                        
                        # Add retry logic for job fetching
                        jobs_response = None
                        for retry in range(max_retries):
                            try:
                                jobs_response = await self.fetch_url_async(
                                    company_jobs_url,
                                    session,
                                    params=jobs_params,
                                    headers=headers,
                                    verify_ssl=False
                                )
                                
                                if jobs_response:
                                    break
                                    
                                logger.warning(f"Job fetch attempt {retry + 1} failed, retrying...")
                                await asyncio.sleep(retry_delay * (retry + 1))
                                
                            except Exception as e:
                                logger.error(f"Error during job fetch attempt {retry + 1}: {str(e)}")
                                if retry < max_retries - 1:
                                    await asyncio.sleep(retry_delay * (retry + 1))
                                else:
                                    logger.error("Max retries exceeded for job fetch")
                                    break
                        
                        if not jobs_response:
                            logger.warning(f"No jobs response for {company_name} at skip {job_skip} after all retries")
                            break
                            
                        try:
                            if isinstance(jobs_response, str):
                                jobs_response = json.loads(jobs_response)
                        except json.JSONDecodeError as e:
                            logger.error(f"Failed to parse jobs response for {company_name}: {str(e)}")
                            break
                        
                        company_jobs = jobs_response.get('entities', [])
                        if not company_jobs:
                            break
                        
                        for job in company_jobs:
                            jobs.append({
                                'company': company_name,
                                'vacancy': job.get('title', 'Unknown Position'),
                                'apply_link': f"https://jobs.glorri.az/vacancies/{company_slug}/{job.get('slug')}/apply"
                            })
                        
                        logger.info(f"Fetched {len(company_jobs)} jobs for {company_name} (batch starting at {job_skip})")
                        
                        if len(company_jobs) < job_limit:
                            break
                        
                        job_skip += job_limit
                        await asyncio.sleep(1)  # Rate limiting between job batches
                
                offset += limit
                if offset >= total_companies:
                    break
                
                await asyncio.sleep(2)  # Rate limiting between company batches
            
            total_jobs = len(jobs)
            logger.info(f"Completed scraping Glorri. Found {total_jobs} total jobs")
            
            return pd.DataFrame(jobs) if jobs else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])
            
        except Exception as e:
            logger.error(f"Unexpected error in Glorri scraper: {str(e)}")
            return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

    @scraper_error_handler
    async def parse_azercell(self, session):
        logger.info("Started scraping Azercell")
        url = "https://www.azercell.com/az/about-us/career.html"
        response_text = await self.fetch_url_async(url, session)
        if not response_text:
            logger.warning("Failed to retrieve Azercell page.")
            return pd.DataFrame()

        soup = BeautifulSoup(response_text, "html.parser")
        vacancies_section = soup.find("section", class_="section_vacancies")
        if not vacancies_section:
            logger.warning("Vacancies section not found on Azercell page.")
            return pd.DataFrame()

        job_listings = vacancies_section.find_all("a", class_="vacancies__link")
        tasks = [self.fetch_url_async(urljoin(url, link["href"]), session) for link in job_listings]
        job_pages = await asyncio.gather(*tasks)

        jobs_data = []
        for i, job_page in enumerate(job_pages):
            if job_page:
                job_soup = BeautifulSoup(job_page, "html.parser")
                jobs_data.append({
                    'company': 'azercell',
                    "vacancy": job_listings[i].find("h4", class_="vacancies__name").text,
                    "location": job_listings[i].find("span", class_="vacancies__location").text.strip(),
                    "apply_link": job_listings[i]["href"],
                    "function": job_soup.find("span", class_="function").text if job_soup.find("span", class_="function") else None,
                    "schedule": job_soup.find("span", class_="schedule").text if job_soup.find("span", class_="schedule") else None,
                    "deadline": job_soup.find("span", class_="deadline").text if job_soup.find("span", class_="deadline") else None,
                    "responsibilities": job_soup.find("div", class_="responsibilities").text.strip() if job_soup.find("div", class_="responsibilities") else None,
                    "requirements": job_soup.find("div", class_="requirements").text.strip() if job_soup.find("div", class_="requirements") else None
                })

        logger.info("Completed scraping Azercell")
        return pd.DataFrame(jobs_data)
    
    @scraper_error_handler
    async def parse_azerconnect(self, session):
        """
        Enhanced Azerconnect scraper with increased timeouts and better connection handling
        """
        logger.info("Started scraping Azerconnect")
        
        base_url = "https://www.azerconnect.az"
        url = f"{base_url}/vacancies"

        # Rotating User-Agent pool
        user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2.1 Safari/605.1.15',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0',
        ]
        
        headers = {
            'User-Agent': random.choice(user_agents),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9,az;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache'
        }

        max_retries = 3
        base_delay = 5

        for attempt in range(max_retries):
            try:
                # Increased timeouts
                timeout = aiohttp.ClientTimeout(
                    total=60,          # Increased total timeout
                    connect=20,        # Increased connection timeout
                    sock_connect=20,   # Socket connection timeout
                    sock_read=30       # Socket read timeout
                )
                
                connector = aiohttp.TCPConnector(
                    ssl=False,
                    limit=1,
                    force_close=True,
                    enable_cleanup_closed=True
                )

                async with aiohttp.ClientSession(
                    timeout=timeout,
                    connector=connector,
                    headers=headers
                ) as client:
                    # First try accessing the homepage
                    try:
                        async with client.get(base_url) as init_response:
                            if init_response.status != 200:
                                logger.error(f"Failed to access homepage (attempt {attempt + 1}): {init_response.status}")
                                delay = base_delay * (2 ** attempt)
                                await asyncio.sleep(delay)
                                continue

                            # Add delay between requests
                            await asyncio.sleep(random.uniform(2, 4))

                            # Update headers for main request
                            headers.update({
                                'Referer': base_url,
                                'Origin': base_url
                            })

                            # Fetch vacancies page
                            async with client.get(url, headers=headers) as response:
                                if response.status != 200:
                                    logger.error(f"Failed to fetch vacancies (attempt {attempt + 1}): {response.status}")
                                    delay = base_delay * (2 ** attempt)
                                    await asyncio.sleep(delay)
                                    continue

                                content = await response.text()
                                
                                if not content or len(content) < 1000:
                                    logger.error(f"Invalid content received (attempt {attempt + 1})")
                                    delay = base_delay * (2 ** attempt)
                                    await asyncio.sleep(delay)
                                    continue

                                soup = BeautifulSoup(content, 'html.parser')
                                job_listings = soup.find_all('div', class_='CollapsibleItem_item__CB3bC')

                                if not job_listings:
                                    logger.error(f"No job listings found (attempt {attempt + 1})")
                                    delay = base_delay * (2 ** attempt)
                                    await asyncio.sleep(delay)
                                    continue

                                jobs_data = []
                                for job in job_listings:
                                    try:
                                        title_block = job.find('div', class_='CollapsibleItem_toggle__XNu5y')
                                        title = title_block.find('span').text.strip() if title_block and title_block.find('span') else None
                                        
                                        if not title:
                                            continue

                                        apply_btn = job.find('a', class_='Button_button-blue__0wZ4l')
                                        apply_link = apply_btn['href'] if apply_btn and 'href' in apply_btn.attrs else None
                                        
                                        if not apply_link:
                                            continue

                                        content_block = job.find('div', class_='CollapsibleItem_contentInner__vVcvk')
                                        if not content_block:
                                            continue

                                        jobs_data.append({
                                            'company': 'Azerconnect',
                                            'vacancy': title,
                                            'apply_link': apply_link
                                        })

                                    except Exception as e:
                                        logger.error(f"Error parsing individual job: {str(e)}")
                                        continue

                                if jobs_data:
                                    logger.info(f"Successfully scraped {len(jobs_data)} jobs from Azerconnect")
                                    return pd.DataFrame(jobs_data)

                    except aiohttp.ClientError as e:
                        logger.error(f"Client error (attempt {attempt + 1}): {str(e)}")
                        if attempt < max_retries - 1:
                            delay = base_delay * (2 ** attempt)
                            await asyncio.sleep(delay)
                            continue
                        return pd.DataFrame()

                    except asyncio.TimeoutError:
                        logger.error(f"Timeout error (attempt {attempt + 1})")
                        if attempt < max_retries - 1:
                            delay = base_delay * (2 ** attempt)
                            await asyncio.sleep(delay)
                            continue
                        return pd.DataFrame()

            except Exception as e:
                logger.error(f"Unexpected error (attempt {attempt + 1}): {str(e)}")
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    await asyncio.sleep(delay)
                    continue
                return pd.DataFrame()

        logger.error("All attempts failed for Azerconnect scraper")
        return pd.DataFrame()
     
    @scraper_error_handler
    async def parse_djinni_co(self, session):
        pages = 17
        logger.info(f"Started scraping djinni.co for the first {pages} pages")

        base_jobs_url = 'https://djinni.co/jobs/'

        jobs = []

        async def scrape_jobs_page(page_url):
            async with session.get(page_url) as response:
                page_response = await response.text()
                soup = BeautifulSoup(page_response, 'html.parser')
                job_items = soup.select('ul.list-unstyled.list-jobs > li')
                for job_item in job_items:
                    job = {}

                    # Extracting company name
                    company_tag = job_item.find('a', class_='text-body')
                    if company_tag:
                        job['company'] = company_tag.text.strip()

                    # Extracting job title
                    title_tag = job_item.find('a', class_='job-item__title-link')
                    if title_tag:
                        job['vacancy'] = title_tag.text.strip()

                    # Extracting application link
                    if title_tag:
                        job['apply_link'] = 'https://djinni.co' + title_tag['href']

                    logger.debug(f"Scraped job: {job}")
                    jobs.append(job)

        # Scrape each page asynchronously
        tasks = []
        for page in range(1, pages + 15):
            logger.info(f"Scraping page {page} for djinni.co")
            page_url = f"{base_jobs_url}?page={page}"
            tasks.append(scrape_jobs_page(page_url))

        await asyncio.gather(*tasks)

        df = pd.DataFrame(jobs, columns=['company', 'vacancy', 'apply_link'])
        logger.info("Scraping completed for djinni.co")

        if df.empty:
            logger.warning("No jobs found during scraping.")
            return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

        for job in df.to_dict('records'):
            logger.debug(f"Title: {job['vacancy']}, Company: {job['company']}, Apply Link: {job['apply_link']}")
            logger.info("=" * 40)

        return df if not df.empty else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])
    
    @scraper_error_handler
    async def parse_abb(self, session):
        logger.info("Scraping starting for ABB")
        base_url = "https://careers.abb-bank.az/api/vacancy/v2/get"
        job_vacancies = []
        page = 0

        while True:
            params = {"page": page}
            response = await self.fetch_url_async(base_url, session, params=params)

            if response:
                try:
                    # Attempt to parse the response as JSON
                    data = response.get("data", [])
                except AttributeError:
                    logger.error("Failed to parse the response as JSON.")
                    break

                if not data:
                    break

                for item in data:
                    title = item.get("title")
                    url = item.get("url")
                    job_vacancies.append({"company": "ABB", "vacancy": title, "apply_link": url})
                page += 1
            else:
                logger.error(f"Failed to retrieve data for page {page}.")
                break

        df = pd.DataFrame(job_vacancies)
        logger.info("ABB scraping completed")
        return df if not df.empty else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])
    
    @scraper_error_handler
    async def parse_busy_az(self, session):
        logger.info("Scraping started for busy.az")
        job_vacancies = []
        for page_num in range(1, 7):
            logger.info(f"Scraping page {page_num}")
            url = f'https://busy.az/vacancies?page={page_num}'
            response = await self.fetch_url_async(url, session)

            if response:
                soup = BeautifulSoup(response, 'html.parser')
                job_listings = soup.find_all('a', class_='job-listing')

                for job in job_listings:
                    job_details = job.find('div', class_='job-listing-details')
                    job_title = job_details.find('h3', class_='job-listing-title').text.strip()
                    company_element = job_details.find('i', class_='icon-material-outline-business')
                    company_name = company_element.find_parent('li').text.strip() if company_element else 'N/A'
                    apply_link = job.get('href')
                    job_vacancies.append({"company": company_name, "vacancy": job_title, "apply_link": apply_link})
            else:
                logger.error(f"Failed to retrieve page {page_num}.")
        df = pd.DataFrame(job_vacancies)
        logger.info("Scraping completed for busy.az")
        return df if not df.empty else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])
    
    @scraper_error_handler
    async def parse_hellojob_az(self, session):
        logger.info("Started scraping of hellojob.az")
        job_vacancies = []
        base_url = "https://www.hellojob.az"

        for page_number in range(1, 13):
            url = f"{base_url}/vakansiyalar?page={page_number}"
            response = await self.fetch_url_async(url, session)
            if response:
                soup = BeautifulSoup(response, 'html.parser')
                job_listings = soup.find_all('a', class_='vacancies__item')
                if not job_listings:
                    logger.info(f"No job listings found on page {page_number}.")
                    continue
                for job in job_listings:
                    company_name = job.find('p', class_='vacancy_item_company').text.strip()
                    vacancy_title = job.find('h3').text.strip()
                    apply_link = job['href'] if job['href'].startswith('http') else base_url + job['href']

                    job_vacancies.append({"company": company_name, "vacancy": vacancy_title, "apply_link": apply_link})
            else:
                logger.warning(f"Failed to retrieve page {page_number}")
        logger.info("Scraping completed for hellojob.az")
        return pd.DataFrame(job_vacancies) if job_vacancies else pd.DataFrame(
            columns=['company', 'vacancy', 'apply_link'])

    @scraper_error_handler
    async def parse_boss_az(self, session):
        logger.info("Starting to scrape Boss.az...")
        job_vacancies = []
        base_url = "https://boss.az"
        
        for page_num in range(1, 10):
            url = f"{base_url}/vacancies?page={page_num}"
            response = await self.fetch_url_async(url, session)
            if response:
                soup = BeautifulSoup(response, 'html.parser')
                job_listings = soup.find_all('div', class_='results-i')
                for job in job_listings:
                    title = job.find('h3', class_='results-i-title').get_text(strip=True)
                    company = job.find('a', class_='results-i-company').get_text(strip=True)
                    link = f"{base_url}{job.find('a', class_='results-i-link')['href']}"
                    job_vacancies.append({"company": company, "vacancy": title, "apply_link": link})
                logger.info(f"Scraped {len(job_listings)} jobs from page {page_num}")
            else:
                logger.warning(f"Failed to retrieve page {page_num}.")
        
        logger.info("Scraping completed for Boss.az")
        return pd.DataFrame(job_vacancies) if job_vacancies else pd.DataFrame(
            columns=['company', 'vacancy', 'apply_link'])

    @scraper_error_handler
    async def parse_ejob_az(self, session):
        start_page = 1
        end_page = 20
        logger.info("Scraping started for ejob.az")
        base_url = "https://ejob.az/is-elanlari"
        all_jobs = []
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Cache-Control': 'max-age=0'
        }
        
        for page in range(start_page, end_page + 1):
            url = f"{base_url}/page-{page}/"
            try:
                response = await self.fetch_url_async(url, session, headers=headers, verify_ssl=False)
                if response:
                    soup = BeautifulSoup(response, 'html.parser')
                    job_tables = soup.find_all('table', class_='background')
                    for job in job_tables:
                        title_link = job.find('a', href=True)
                        company = job.find('div', class_='company')
                        if title_link and company:
                            all_jobs.append({
                                'company': company.text.strip(),
                                'vacancy': title_link.text.strip(),
                                'apply_link': f"https://ejob.az{title_link['href']}"
                            })
                else:
                    logger.warning(f"Failed to retrieve page {page}.")
            except Exception as e:
                logger.error(f"Error on page {page}: {e}")
                continue

        logger.info("Scraping completed for ejob.az")
        return pd.DataFrame(all_jobs) if all_jobs else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])
    
    @scraper_error_handler
    async def parse_vakansiya_az(self, session):
        logger.info("Scraping started for vakansiya.az")
        url = 'https://www.vakansiya.az/az/'
        response = await self.fetch_url_async(url, session)
        
        if response:
            soup = BeautifulSoup(response, 'html.parser')
            jobs = []
            job_divs = soup.find_all('div', id='js-jobs-wrapper')

            for job_div in job_divs:
                company = job_div.find_all('div', class_='js-fields')[1].find('a')
                title = job_div.find('a', class_='jobtitle')
                apply_link = title['href'] if title else None

                jobs.append({
                    'company': company.get_text(strip=True) if company else 'N/A',
                    'vacancy': title.get_text(strip=True) if title else 'N/A',
                    'apply_link': f'https://www.vakansiya.az{apply_link}' if apply_link else 'N/A'
                })

            logger.info("Scraping completed for vakansiya.az")
            return pd.DataFrame(jobs) if jobs else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])
        else:
            logger.error("Failed to retrieve the page.")
            return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])
        
    @scraper_error_handler
    async def parse_ishelanlari_az(self, session):
        logger.info("Scraping started for ishelanlari.az")
        url = "https://ishelanlari.az/az/vacancies//0/360/"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'}
        response = await self.fetch_url_async(url, session, params=None, verify_ssl=True)

        if response:
            soup = BeautifulSoup(response, 'html.parser')
            vacancies = []
            for job in soup.find_all("div", class_="card-body"):
                title_element = job.find("h2", class_="font-weight-bold")
                company_element = job.find("a", class_="text-muted")
                details_link_element = job.find("a", class_="position-absolute")

                title = title_element.text.strip() if title_element else "No title provided"
                company = company_element.text.strip() if company_element else "No company provided"
                link = details_link_element["href"] if details_link_element else "No link provided"

                vacancies.append({
                    "company": company,
                    "vacancy": title,
                    "apply_link": "https://ishelanlari.az" + link
                })

            logger.info("Scraping completed for ishelanlari.az")
            return pd.DataFrame(vacancies) if vacancies else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])
        else:
            logger.error("Failed to retrieve data for ishelanlari.az.")
            return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])
        
    @scraper_error_handler
    async def parse_banker_az(self, session):
        logger.info("Started scraping Banker.az")
        base_url = 'https://banker.az/vakansiyalar'
        num_pages = 5

        all_job_titles = []
        all_company_names = []
        all_apply_links = []

        for page in range(1, num_pages + 1):
            url = f"{base_url}/page/{page}/"
            response = await self.fetch_url_async(url, session)

            if response:
                soup = BeautifulSoup(response, 'html.parser')
                job_listings = soup.find_all('div', class_='list-data')

                for job in job_listings:
                    job_info = job.find('div', class_='job-info')
                    title_tag = job_info.find('a') if job_info else None
                    title = title_tag.text.strip() if title_tag else None
                    link = title_tag['href'] if title_tag else None

                    company_logo = job.find('div', class_='company-logo')
                    company_img = company_logo.find('img') if company_logo else None
                    company = company_img.get('alt') if company_img else None

                    if title and '-' in title:
                        title_parts = title.split(' – ')
                        title = title_parts[0].strip()
                        if len(title_parts) > 1:
                            company = title_parts[1].strip()

                    if title and company and link:
                        all_job_titles.append(title)
                        all_company_names.append(company)
                        all_apply_links.append(link)
            else:
                logger.warning(f"Failed to retrieve page {page}.")

        df = pd.DataFrame({'company': all_company_names, 'vacancy': all_job_titles, 'apply_link': all_apply_links})
        logger.info("Scraping completed for Banker.az")
        return df if not df.empty else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

    @scraper_error_handler
    async def parse_offer_az(self, session):
        logger.info("Started scraping offer.az")
        base_url = "https://www.offer.az/is-elanlari/page/"
        all_jobs = []

        for page_number in range(1, 8):
            url = f"{base_url}{page_number}/"
            response = await self.fetch_url_async(url, session)

            if response:
                soup = BeautifulSoup(response, 'html.parser')
                job_cards = soup.find_all('div', class_='job-card')

                for job_card in job_cards:
                    title_tag = job_card.find('a', class_='job-card__title')
                    title = title_tag.text.strip() if title_tag else "N/A"
                    link = title_tag['href'] if title_tag else "N/A"
                    company_tag = job_card.find('p', class_='job-card__meta')
                    company = company_tag.text.strip() if company_tag else "N/A"

                    all_jobs.append({
                        'vacancy': title,
                        'company': company,
                        'location': 'N/A',  # Placeholder, as location is not extracted
                        'apply_link': link,
                        'description': job_card.find('p', class_='job-card__excerpt').text.strip() if job_card.find('p', class_='job-card__excerpt') else "N/A"
                    })
            else:
                logger.warning(f"Failed to retrieve page {page_number}. Retrying...")
                # Retry mechanism
                retry_response = await self.fetch_url_async(url, session)
                if retry_response:
                    soup = BeautifulSoup(retry_response, 'html.parser')
                    job_cards = soup.find_all('div', class_='job-card')

                    for job_card in job_cards:
                        title_tag = job_card.find('a', class_='job-card__title')
                        title = title_tag.text.strip() if title_tag else "N/A"
                        link = title_tag['href'] if title_tag else "N/A"
                        company_tag = job_card.find('p', class_='job-card__meta')
                        company = company_tag.text.strip() if company_tag else "N/A"

                        all_jobs.append({
                            'vacancy': title,
                            'company': company,
                            'location': 'N/A',
                            'apply_link': link,
                            'description': job_card.find('p', class_='job-card__excerpt').text.strip() if job_card.find('p', class_='job-card__excerpt') else "N/A"
                        })
                else:
                    logger.error(f"Failed to retrieve page {page_number} after retrying.")

        logger.info("Scraping completed for offer.az")
        return pd.DataFrame(all_jobs) if all_jobs else pd.DataFrame(columns=['vacancy', 'company', 'location', 'apply_link', 'description'])

    @scraper_error_handler
    async def parse_smartjob_az(self, session):
        logger.info("Started scraping SmartJob.az")
        jobs = []
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'en-GB,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Referer': 'https://smartjob.az/',
            'Connection': 'keep-alive',
            'Sec-Ch-Ua': '"Not A(Brand";v="99", "Google Chrome";v="121", "Chromium";v="121"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"macOS"',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-origin',
            'Sec-Fetch-User': '?1',
            'Upgrade-Insecure-Requests': '1',
            'Cache-Control': 'max-age=0',
            'DNT': '1'
        }

        for page in range(1, 5):
            url = f"https://smartjob.az/vacancies?page={page}"
            try:
                # Add delay between requests
                await asyncio.sleep(random.uniform(2, 4))
                
                response = await self.fetch_url_async(
                    url, 
                    session, 
                    headers=headers,
                    # ssl=False  # Disable SSL verification if needed
                )

                if response:
                    soup = BeautifulSoup(response, "html.parser")
                    job_listings = soup.find_all('div', class_='item-click')

                    if not job_listings:
                        logger.info(f"No job listings found on page {page}.")
                        continue

                    for listing in job_listings:
                        try:
                            title_elem = listing.find('div', class_='brows-job-position')
                            if title_elem and title_elem.h3 and title_elem.h3.a:
                                title = title_elem.h3.a.text.strip()
                                apply_link = title_elem.h3.a['href']
                                company_elem = listing.find('span', class_='company-title')
                                company = company_elem.a.text.strip() if company_elem and company_elem.a else "Unknown"
                                
                                jobs.append({
                                    'company': company,
                                    'vacancy': title,
                                    'apply_link': apply_link
                                })
                        except AttributeError as e:
                            logger.warning(f"Error parsing job listing: {e}")
                            continue
                else:
                    logger.warning(f"Failed to retrieve page {page}.")
                    # Add exponential backoff
                    await asyncio.sleep(2 ** page)
                    
            except aiohttp.ClientConnectorError as e:
                logger.error(f"Connection error on page {page}: {e}")
                await asyncio.sleep(5)  # Longer delay on connection error
                continue  # Try next page instead of breaking
                
            except Exception as e:
                logger.error(f"An error occurred on page {page}: {e}")
                await asyncio.sleep(3)
                continue

        logger.info("Scraping completed for SmartJob.az")
        return pd.DataFrame(jobs) if jobs else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])


    @scraper_error_handler
    async def parse_isveren_az(self, session):
        start_page = 1
        end_page = 15
        max_retries = 3
        backoff_factor = 1
        jobs = []

        for page_num in range(start_page, end_page + 1):
            retries = 0
            while retries < max_retries:
                try:
                    logger.info(f"Scraping started for isveren.az page {page_num}")
                    url = f"https://isveren.az/?page={page_num}"
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15',
                    }

                    response = await self.fetch_url_async(url, session, headers=headers, verify_ssl=False)

                    if response:
                        soup = BeautifulSoup(response, 'html.parser')
                        job_cards = soup.find_all('div', class_='job-card')

                        for job_card in job_cards:
                            title_element = job_card.find('h5', class_='job-title')
                            company_element = job_card.find('p', class_='job-list')
                            link_element = job_card.find('a', href=True)

                            title = title_element.text.strip() if title_element else "No title provided"
                            company = company_element.text.strip() if company_element else "No company provided"
                            link = link_element['href'] if link_element else "No link provided"

                            jobs.append({
                                'company': company,
                                'vacancy': title,
                                'apply_link': link
                            })

                        break  # Exit the retry loop if the request was successful
                    else:
                        logger.error(f"Failed to retrieve page {page_num}.")
                        break

                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    retries += 1
                    logger.warning(f"Attempt {retries} for page {page_num} failed: {e}")
                    if retries < max_retries:
                        sleep_time = backoff_factor * (2 ** (retries - 1))
                        logger.info(f"Retrying page {page_num} in {sleep_time} seconds...")
                        await asyncio.sleep(sleep_time)
                    else:
                        logger.error(f"Max retries exceeded for page {page_num}")

        df = pd.DataFrame(jobs)
        logger.info("Scraping completed for isveren.az")
        return df if not df.empty else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

    @scraper_error_handler
    async def parse_isqur(self, session):
        start_page = 1
        end_page = 5
        logger.info("Started scraping isqur.com")
        job_vacancies = []
        base_url = "https://isqur.com/is-elanlari/sehife-"

        for page_num in range(start_page, end_page + 1):
            logger.info(f"Scraping page {page_num} for isqur.com")
            url = f"{base_url}{page_num}"
            response = await self.fetch_url_async(url, session)
            if response:
                soup = BeautifulSoup(response, 'html.parser')
                job_cards = soup.find_all('div', class_='kart')
                for job in job_cards:
                    title = job.find('div', class_='basliq').text.strip()
                    company = "Unknown"  # The provided HTML does not include a company name
                    link = "https://isqur.com/" + job.find('a')['href']
                    job_vacancies.append({'company': company, 'vacancy': title, 'apply_link': link})
            else:
                logger.error(f"Failed to retrieve page {page_num} for isqur.com")

        logger.info("Scraping completed for isqur.com")
        return pd.DataFrame(job_vacancies) if job_vacancies else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

    async def parse_kapitalbank(self, session):
        """Fixed Kapital Bank scraper"""
        logger.info("Fetching jobs from Kapital Bank API")
        url = "https://apihr.kapitalbank.az/api/Vacancy/vacancies?Skip=0&Take=150&SortField=id&OrderBy=true"
        response = await self.fetch_url_async(url, session)

        if response:
            data = await self.parse_json_response(response)
            jobs_data = data.get('data', [])
            
            if not jobs_data:
                logger.warning("No job data found in the API response.")
                return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

            jobs = [{
                'company': 'Kapital Bank',
                'vacancy': job['header'],
                'apply_link': f"https://hr.kapitalbank.az/vacancy/{job['id']}"
            } for job in jobs_data]

            logger.info("Job data fetched and parsed successfully from Kapital Bank API")
            return pd.DataFrame(jobs)
        else:
            logger.error("Failed to fetch data from Kapital Bank API.")
            return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

    
    @scraper_error_handler
    async def parse_bank_of_baku_az(self, session):
        logger.info("Scraping started for Bank of Baku")
        url = "https://careers.bankofbaku.com/az/vacancies"
        response = await self.fetch_url_async(url, session, verify_ssl=False)

        if response:
            soup = BeautifulSoup(response, 'html.parser')
            jobs = []
            job_blocks = soup.find_all('div', class_='main-cell mc-50p')

            for job_block in job_blocks:
                link_tag = job_block.find('a')
                if link_tag:
                    link = 'https://careers.bankofbaku.com' + link_tag['href']
                    job_info = job_block.find('div', class_='vacancy-list-block-content')
                    title = job_info.find('div', class_='vacancy-list-block-header').get_text(
                        strip=True) if job_info else 'No title provided'
                    department_label = job_info.find('label', class_='light-red-bg')
                    deadline = department_label.get_text(strip=True) if department_label else 'No deadline listed'
                    department_info = job_info.find_all('label')[0].get_text(strip=True) if len(
                        job_info.find_all('label')) > 0 else 'No department listed'
                    location_info = job_info.find_all('label')[1].get_text(strip=True) if len(
                        job_info.find_all('label')) > 1 else 'No location listed'

                    jobs.append({'company': 'Bank of Baku', 'vacancy': title, 'apply_link': link})

            logger.info("Scraping completed for Bank of Baku")
            return pd.DataFrame(jobs) if jobs else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])
        else:
            logger.error("Failed to retrieve data for Bank of Baku.")
            return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

    @scraper_error_handler
    async def parse_jobbox_az(self, session):
        start_page=1
        end_page=10
        logger.info(f"Scraping started for jobbox.az from page {start_page} to page {end_page}")
        start_page=1
        end_page=5
        job_vacancies = []
        for page_num in range(start_page, end_page + 1):
            logger.info(f"Scraping page {page_num}")
            url = f'https://jobbox.az/az/vacancies?page={page_num}'
            response = await self.fetch_url_async(url, session)

            if response:
                soup = BeautifulSoup(response, 'html.parser')
                job_items = soup.find_all('li', class_='item')

                for item in job_items:
                    job = {}

                    link_tag = item.find('a')
                    if link_tag:
                        job['apply_link'] = link_tag['href']
                    else:
                        continue  # Skip if no link found

                    title_ul = item.find('ul', class_='title')
                    if title_ul:
                        title_div = title_ul.find_all('li')
                        job['vacancy'] = title_div[0].text.strip() if len(title_div) > 0 else None
                    else:
                        continue  # Skip if title information is missing

                    address_ul = item.find('ul', class_='address')
                    if address_ul:
                        address_div = address_ul.find_all('li')
                        job['company'] = address_div[0].text.strip() if len(address_div) > 0 else None
                    else:
                        continue  # Skip if address information is missing

                    job_vacancies.append(job)
            else:
                logger.error(f"Failed to retrieve page {page_num}.")

        df = pd.DataFrame(job_vacancies, columns=['company', 'vacancy', 'apply_link'])
        logger.info("Scraping completed for jobbox.az")
        logger.info(df)
        return df if not df.empty else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

    async def parse_vakansiya_biz(self, session):
        """Fixed Vakansiya.biz scraper"""
        logger.info("Started scraping Vakansiya.biz")
        base_url = "https://api.vakansiya.biz/api/v1/vacancies/search"
        headers = {'Content-Type': 'application/json'}
        page = 1
        all_jobs = []

        while True:
            response = await self.fetch_url_async(
                f"{base_url}?page={page}&country_id=108&city_id=0&industry_id=0&job_type_id=0&work_type_id=0&gender=-1&education_id=0&experience_id=0&min_salary=0&max_salary=0&title=",
                session,
                headers=headers
            )

            if not response:
                logger.error(f"Failed to fetch page {page}")
                break

            data = await self.parse_json_response(response)
            jobs_data = data.get('data', [])
            all_jobs.extend(jobs_data)

            if not data.get('next_page_url'):
                break

            page += 1

        job_listings = [{
            'company': job['company_name'].strip().lower(),
            'vacancy': job['title'].strip().lower(),
            'apply_link': f"https://vakansiya.biz/az/vakansiyalar/{job['id']}/{job['slug']}"
        } for job in all_jobs]

        df = pd.DataFrame(job_listings)
        logger.info("Scraping completed for Vakansiya.biz")
        return df if not df.empty else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])


    @scraper_error_handler
    async def parse_its_gov(self, session):
        start_page = 1
        end_page = 20
        logger.info(f"Scraping its.gov.az from page {start_page} to page {end_page}")
        base_url = "https://its.gov.az/page/vakansiyalar?page="
        all_vacancies = []

        for page in range(start_page, end_page + 1):
            url = f"{base_url}{page}"
            logger.info(f"Fetching page {page}")
            response = await self.fetch_url_async(url, session)
            
            if response:
                soup = BeautifulSoup(response, "html.parser")
                events = soup.find_all('div', class_='event')
                if not events:
                    logger.info(f"No job listings found on page {page}")
                    break

                for event in events:
                    title_tag = event.find('a', class_='event__link')
                    if title_tag:
                        title = title_tag.get_text(strip=True).lower()
                        link = title_tag['href']
                        deadline_tag = event.find('span', class_='event__time')
                        deadline = deadline_tag.get_text(strip=True) if deadline_tag else 'N/A'
                        all_vacancies.append({
                            'company': 'icbari tibbi sigorta',  # Normalized company name
                            'vacancy': title,
                            'apply_link': link
                        })
            else:
                logger.warning(f"Failed to retrieve page {page}")

        df = pd.DataFrame(all_vacancies)
        logger.info("Scraping completed for its.gov.az")
        return df if not df.empty else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

    @scraper_error_handler
    async def parse_is_elanlari_iilkin(self, session):
        logger.info("Started scraping is-elanlari.iilkin.com")
        base_url = 'http://is-elanlari.iilkin.com/vakansiyalar/'
        job_listings = []

        async def scrape_page(content):
            soup = BeautifulSoup(content, 'html.parser')
            main_content = soup.find('main', id='main', class_='site-main')
            if main_content:
                articles = main_content.find_all('article')
                for job in articles:
                    title_element = job.find('a', class_='home-title-links')
                    company_element = job.find('p', class_='vacan-company-name')
                    link_element = job.find('a', class_='home-title-links')

                    job_listings.append({
                        "vacancy": title_element.text.strip().lower() if title_element else 'n/a',
                        "company": company_element.text.strip().lower() if company_element else 'n/a',
                        "apply_link": link_element['href'] if link_element else 'n/a'
                    })
            else:
                logger.warning("Main content not found")

        for page_num in range(1, 4):
            url = base_url if page_num == 1 else f'{base_url}{page_num}'
            logger.info(f'Scraping page {page_num}...')
            response = await self.fetch_url_async(url, session)
            if response:
                await scrape_page(response)
            else:
                logger.warning(f"Failed to retrieve page {page_num} for is-elanlari.iilkin.com")

        if job_listings:
            df = pd.DataFrame(job_listings)
            logger.info("Scraping completed for is-elanlari.iilkin.com")
            return df
        else:
            logger.warning("No job listings found")
            return pd.DataFrame(columns=['vacancy', 'company', 'apply_link'])


    @scraper_error_handler
    async def parse_tabib_vacancies(self, session):
        logger.info("Started scraping TABIB vacancies")
        url = "https://tabib.gov.az/vetendashlar-ucun/vakansiyalar"  # Updated URL
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html'
        }
        
        try:
            response = await self.fetch_url_async(url, session, headers=headers)
            if response:
                soup = BeautifulSoup(response, 'html.parser')
                jobs = []
                
                # Find vacancy containers
                vacancy_items = soup.find_all('div', class_='vacancy-item')
                for item in vacancy_items:
                    title = item.find('h2', class_='vacancy-title')
                    link = item.find('a', class_='apply-link')
                    
                    if title and link:
                        jobs.append({
                            'company': 'TABIB',
                            'vacancy': title.text.strip(),
                            'apply_link': urljoin(url, link['href'])
                        })
                
                return pd.DataFrame(jobs)
        except Exception as e:
            logger.error(f"Error scraping TABIB: {e}")
        return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

    @scraper_error_handler
    async def parse_projobs_vacancies(self, session):
        """Fetch and parse job vacancies from Projobs API with improved error handling."""
        data = []
        base_url = "https://core.projobs.az/v1/vacancies"
        headers = {
            'Accept': '*/*',
            'Accept-Encoding': 'gzip, deflate, br, zstd',
            'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8,ru;q=0.7,az;q=0.6',
            'Connection': 'keep-alive',
            'Dnt': '1',
            'Host': 'core.projobs.az',
            'Origin': 'https://projobs.az',
            'Referer': 'https://projobs.az/',
            'Sec-Ch-Ua': '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"macOS"',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-site',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
        }
        max_pages = 10
        
        for page in range(1, max_pages + 1):
            url = f"{base_url}?page={page}"
            try:
                response = await self.fetch_url_async(url, session, headers=headers)
                if not response:
                    logger.error(f"Empty response from {url}")
                    continue
                    
                json_data = await self.parse_json_response(response)
                if not json_data:
                    logger.error(f"Failed to parse JSON from {url}")
                    continue
                    
                vacancies = json_data.get("data", [])
                if not vacancies:
                    logger.warning(f"No vacancies found on page {page}")
                    continue
                    
                for vacancy in vacancies:
                    try:
                        vacancy_info = {
                            "company": vacancy.get("companyName", "Unknown"),
                            "vacancy": vacancy.get("name", "Unknown Position"),
                            "apply_link": f"https://projobs.az/jobdetails/{vacancy.get('id', '')}"
                        }
                        if all(vacancy_info.values()):
                            data.append(vacancy_info)
                    except Exception as e:
                        logger.error(f"Error processing vacancy: {str(e)}")
                        continue
                        
                logger.info(f"Successfully scraped page {page}")
                
            except Exception as e:
                logger.error(f"Error processing page {page}: {str(e)}")
                continue

        if data:
            df = pd.DataFrame(data)
            logger.info(f"Successfully scraped {len(data)} jobs from Projobs")
            return df
        else:
            logger.warning("No vacancies found")
            return pd.DataFrame(columns=["company", "vacancy", "apply_link"])

    @scraper_error_handler
    async def parse_azergold(self, session):
        logger.info("Started scraping AzerGold")
        url = "https://careers.azergold.az/"
        response = await self.fetch_url_async(url, session, verify_ssl=False)  # Handling SSL issues with verify_ssl=False

        if response:
            soup = BeautifulSoup(response, "html.parser")
            logger.info("Page fetched successfully")

            # Locate the table containing the job listings
            table = soup.find("table", class_="table-vacancy")
            if table:
                logger.info("Vacancies section found")
                job_rows = table.find("tbody").find_all("tr")

                job_titles = []
                job_links = []

                for row in job_rows:
                    title_cell = row.find("td")
                    if title_cell:
                        title_link = title_cell.find("a")
                        if title_link:
                            job_titles.append(title_link.text.strip())
                            job_links.append(title_link["href"])

                df = pd.DataFrame({'company': 'AzerGold', "vacancy": job_titles, "apply_link": job_links})
                logger.info("Scraping completed for AzerGold")
                return df if not df.empty else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])
            else:
                logger.warning("Vacancies section not found on the AzerGold page.")
        else:
            logger.error("Failed to fetch the AzerGold page.")

        return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

    @scraper_error_handler
    async def parse_konsis(self, session):
        logger.info("Started scraping Konsis")
        url = "https://konsis.az/karyera-vakansiya/"
        response = await self.fetch_url_async(url, session, verify_ssl=False)  # Handling SSL issues with verify_ssl=False

        if response:
            soup = BeautifulSoup(response, "html.parser")
            logger.info("Page fetched successfully")

            # Locate the articles containing the job listings
            articles = soup.find_all("div", class_="grid-item")
            if articles:
                logger.info("Vacancies section found")
                job_titles = []
                job_companies = []
                job_locations = []
                job_types = []
                job_descriptions = []
                job_links = []

                for article in articles:
                    meta = article.find("div", class_="item--meta")
                    if meta:
                        job_title = meta.find("h3", class_="item--title").text.strip()
                        features = meta.find_all("li")
                        job_company = features[0].text.strip() if len(features) > 0 else "N/A"
                        job_location = features[1].text.strip() if len(features) > 1 else "N/A"
                        job_type = features[2].text.strip() if len(features) > 2 else "N/A"
                        job_description = article.find("div", class_="item-desc").text.strip()
                        job_link = article.find("a", class_="btn btn-secondary", href=True)["href"]

                        job_titles.append(job_title)
                        job_companies.append(job_company)
                        job_locations.append(job_location)
                        job_types.append(job_type)
                        job_descriptions.append(job_description)
                        job_links.append("https://konsis.az" + job_link)

                df = pd.DataFrame({
                    'company': job_companies,
                    'vacancy': job_titles,
                    'apply_link': job_links
                })
                logger.info("Scraping completed for Konsis")
                return df if not df.empty else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])
            else:
                logger.warning("Vacancies section not found on the Konsis page.")
        else:
            logger.error("Failed to fetch the Konsis page.")

        return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

    @scraper_error_handler
    async def parse_baku_electronics(self, session):
        logger.info("Started scraping Baku Electronics")
        base_url = "https://careers.bakuelectronics.az/az/vacancies/?p="
        all_job_titles = []
        all_job_categories = []
        all_job_locations = []
        all_job_deadlines = []
        all_job_links = []

        for page in range(1, 3):
            url = f"{base_url}{page}"
            response = await self.fetch_url_async(url, session, verify_ssl=False)
            if response:
                soup = BeautifulSoup(response, "html.parser")
                logger.info(f"Page {page} fetched successfully")

                # Locate the blocks containing the job listings
                vacancy_blocks = soup.find_all("div", class_="vacancy-list-block")
                if vacancy_blocks:
                    logger.info("Vacancies section found")
                    for block in vacancy_blocks:
                        header = block.find("div", class_="vacancy-list-block-header")
                        info = block.find("div", class_="vacancy-list-block-info")
                        deadline = block.find("div", class_="vacancy-list-block-note")
                        link_tag = block.find_parent("a", href=True)
                        link = link_tag["href"] if link_tag else None

                        job_title = header.text.strip() if header else None
                        category_location = info.find_all("label") if info else []
                        job_category = category_location[0].text.strip() if len(category_location) > 0 else None
                        job_location = category_location[1].text.strip() if len(category_location) > 1 else None
                        job_deadline = deadline.text.strip() if deadline else None
                        job_link = "https://careers.bakuelectronics.az" + link if link else None

                        if None in [job_title, job_category, job_location, job_deadline, job_link]:
                            logger.warning(f"Missing elements in block: title={job_title}, category={job_category}, location={job_location}, deadline={job_deadline}, link={job_link}")
                            continue

                        all_job_titles.append(job_title)
                        all_job_categories.append(job_category)
                        all_job_locations.append(job_location)
                        all_job_deadlines.append(job_deadline)
                        all_job_links.append(job_link)
                else:
                    logger.warning(f"Vacancies section not found on page {page}.")
            else:
                logger.warning(f"Failed to fetch page {page}.")

        df = pd.DataFrame({
            'company': 'Baku Electronics',
            'vacancy': all_job_titles,
            'apply_link': all_job_links
        })
        logger.info("Scraping completed for Baku Electronics")
        return df if not df.empty else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

    @scraper_error_handler
    async def parse_asco(self, session):
        logger.info("Started scraping ASCO")
        base_url = "https://www.asco.az/az/pages/6/65?page="
        all_job_numbers = []
        all_job_titles = []
        all_job_deadlines = []
        all_job_links = []

        for page in range(1, 4):
            url = f"{base_url}{page}"
            response = await self.fetch_url_async(url, session, verify_ssl=False)
            if response:
                soup = BeautifulSoup(response, "html.parser")
                logger.info(f"Page {page} fetched successfully")

                # Locate the blocks containing the job listings
                table = soup.find("table", class_="default")
                if table:
                    rows = table.find_all("tr")[1:]  # Skip header row
                    for row in rows:
                        cols = row.find_all("td")
                        job_number = cols[0].text.strip() if cols[0] else None
                        job_title = cols[1].text.strip() if cols[1] else None
                        job_deadline = cols[2].text.strip() if cols[2] else None
                        job_link_tag = cols[3].find("a", href=True)
                        job_link = job_link_tag["href"] if job_link_tag else None

                        if None in [job_number, job_title, job_deadline, job_link]:
                            logger.warning(f"Missing elements in row: number={job_number}, title={job_title}, deadline={job_deadline}, link={job_link}")
                            continue

                        all_job_numbers.append(job_number)
                        all_job_titles.append(job_title)
                        all_job_deadlines.append(job_deadline)
                        all_job_links.append(job_link)
                else:
                    logger.warning(f"Job listings table not found on page {page}.")
            else:
                logger.warning(f"Failed to fetch page {page}.")

        df = pd.DataFrame({
            'company': 'ASCO',
            'vacancy': all_job_titles,
            'apply_link': all_job_links
        })
        logger.info("Scraping completed for ASCO")
        return df if not df.empty else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

    @scraper_error_handler
    async def parse_cbar(self, session):
        logger.info("Started scraping CBAR")
        url = "https://www.cbar.az/hr/f?p=100:106"
        response = await self.fetch_url_async(url, session, verify_ssl=False)
        if response:
            soup = BeautifulSoup(response, "html.parser")
            logger.info("Page fetched successfully")

            all_job_numbers = []
            all_job_titles = []
            all_job_start_dates = []
            all_job_end_dates = []
            all_job_links = []

            # Locate the blocks containing the job listings
            table = soup.find("table", class_="a-IRR-table")
            if table:
                rows = table.find_all("tr")[1:]  # Skip header row
                for row in rows:
                    cols = row.find_all("td")
                    job_number = cols[0].text.strip() if cols[0] else None
                    job_title = cols[1].text.strip() if cols[1] else None
                    job_start_date = cols[2].text.strip() if cols[2] else None
                    job_end_date = cols[3].text.strip() if cols[3] else None
                    job_link_tag = cols[1].find("a", href=True)
                    job_link = job_link_tag["href"] if job_link_tag else None

                    if job_link and 'javascript:' in job_link:
                        match = re.search(r"P50_VACANCY_ID,P50_POSITION_ID:([^,]+),([^&]+)", job_link)
                        if match:
                            job_vacancy_id = match.group(1)
                            job_position_id = match.group(2)
                            job_link = f"https://www.cbar.az/hr/f?p=100:50::::50:P50_VACANCY_ID,P50_POSITION_ID:{job_vacancy_id},{job_position_id}"

                    if None in [job_number, job_title, job_start_date, job_end_date, job_link]:
                        logger.warning(f"Missing elements in row: number={job_number}, title={job_title}, start_date={job_start_date}, end_date={job_end_date}, link={job_link}")
                        continue

                    all_job_numbers.append(job_number)
                    all_job_titles.append(job_title)
                    all_job_start_dates.append(job_start_date)
                    all_job_end_dates.append(job_end_date)
                    all_job_links.append(job_link)
            else:
                logger.warning("Job listings table not found on the page.")

            df = pd.DataFrame({
                'company': 'CBAR',
                'vacancy': all_job_titles,
                'apply_link': 'https://www.cbar.az/hr/f?p=100:106'
            })
            logger.info("Scraping completed for CBAR")
            return df if not df.empty else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])
        else:
            logger.error("Failed to fetch the page.")
            return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

    @scraper_error_handler
    async def parse_ada(self, session):
        logger.info("Started scraping ADA University")

        url = "https://ada.edu.az/jobs"
        response = await self.fetch_url_async(url, session, verify_ssl=False)  # SSL disabled for this connection

        if response:
            soup = BeautifulSoup(response, 'html.parser')

            # Find the table containing the job listings
            table = soup.find('table', class_='table-job')
            jobs = []

            if table:
                # Loop through each row in the table body
                for row in table.find('tbody').find_all('tr'):
                    title_tag = row.find('td', class_='name').find('a')
                    view_link_tag = row.find('td', class_='view').find('a')

                    # Safely get the title and apply link
                    title = title_tag.text.strip() if title_tag else "N/A"
                    apply_link = view_link_tag['href'] if view_link_tag else "N/A"

                    job = {
                        'company': 'ADA University',
                        'vacancy': title,
                        'apply_link': apply_link
                    }
                    jobs.append(job)

                df = pd.DataFrame(jobs)
                logger.info("Scraping completed for ADA University")
                logger.info(f"Scraped jobs: {len(jobs)}")
                return df if not df.empty else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])
            else:
                logger.warning("No job listings found on the ADA University page.")
                return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])
        else:
            logger.error("Failed to fetch the page.")
            return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

    @scraper_error_handler
    async def parse_jobfinder(self, session):
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        all_job_data = []

        start_page = 1
        end_page = 10

        for page_number in range(start_page, end_page + 1):
            url = f"https://jobfinder.az/job?page={page_number}"
            response = await self.fetch_url_async(url, session, headers=headers)

            if not response:
                logger.error(f"Failed to retrieve page {page_number}")
                continue

            soup = BeautifulSoup(response, 'html.parser')
            job_listings = soup.find_all('div', class_='content_list_item job_list_item clearfix')

            for job in job_listings:
                title_tag = job.find('h3', class_='value').find('a')
                company_tag = job.find('div', class_='jobListCompany')
                salary_tag = job.find('div', class_='salaryBox')
                job_infos = job.find('div', class_='jobInfos').find_all('span', class_='jobSchedule')

                all_job_data.append({
                    'company': company_tag.find('img')['alt'] if company_tag and company_tag.find('img') else 'N/A',
                    'vacancy': title_tag.text.strip() if title_tag else 'N/A',
                    'apply_link': 'https://jobfinder.az' + title_tag['href'] if title_tag else 'N/A'
                })

        if all_job_data:
            df = pd.DataFrame(all_job_data)
            logger.info("Scraping completed for JobFinder")
            return df
        else:
            logger.warning("No job listings found on JobFinder.")
            return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

    @scraper_error_handler
    async def scrape_regulator(self, session):
        url = "https://regulator.gov.az/az/vakansiyalar/vakansiyalar_611"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

        response = await self.fetch_url_async(url, session, headers=headers, verify_ssl=False)

        if not response:
            logger.error("Failed to fetch data from regulator.gov.az")
            return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

        soup = BeautifulSoup(response, 'html.parser')
        table = soup.find('table', {'border': '1'})

        if not table:
            logger.warning("No table found on the regulator.gov.az page.")
            return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

        rows = table.find_all('tr')[1:]  # Skip the header row
        job_data = []

        for row in rows:
            cols = row.find_all('td')
            title_tag = cols[0].find('a')
            title = title_tag.text.strip() if title_tag else 'N/A'
            location = cols[1].text.strip() if len(cols) > 1 else 'N/A'
            field = cols[2].text.strip() if len(cols) > 2 else 'N/A'
            deadline = cols[3].text.strip() if len(cols) > 3 else 'N/A'
            apply_link = title_tag['href'] if title_tag else 'N/A'

            job_data.append({
                'company': 'Azerbaijan Energy Regulatory Agency',
                'vacancy': title,
                'apply_link': apply_link
            })

        df = pd.DataFrame(job_data)
        logger.info("Scraping completed for regulator.gov.az")
        return df

    @scraper_error_handler
    async def scrape_ekaryera(self, session):
        page_limit = 5
        base_url = "https://www.ekaryera.az/vakansiyalar?page="
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

        job_data = []

        for page in range(1, page_limit + 1):
            url = base_url + str(page)
            response = await self.fetch_url_async(url, session, headers=headers, verify_ssl=False)

            if not response:
                logger.error(f"Failed to fetch page {page} from ekaryera.az")
                continue

            soup = BeautifulSoup(response, 'html.parser')
            job_list = soup.find('div', {'class': 'job-listings-sec'}).find_all('div', {'class': 'job-listing'})

            for job in job_list:
                job_title = job.find('h3').find('a').text.strip()
                company = job.find('span', text=True).text.strip() if job.find('span', text=True) else 'CompanyName'
                location = job.find('div', {'class': 'job-lctn'}).text.strip()
                employment_type = job.find('span', {'class': 'job-is'}).text.strip()
                experience = job.find('i').text.strip()
                apply_link = job.find('a')['href']

                job_data.append({
                    'company': company,
                    'vacancy': job_title,
                    'apply_link': apply_link
                })
            logger.info(f"Scraped page {page} for ekaryera.az")

        df = pd.DataFrame(job_data)
        logger.info("Scraping completed for ekaryera.az")
        return df if not df.empty else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

    @scraper_error_handler
    async def scrape_bravosupermarket(self, session):
        base_url = "https://www.bravosupermarket.az/career/all-vacancies/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

        job_data = []

        response = await self.fetch_url_async(base_url, session, headers=headers, verify_ssl=True)
        if not response:
            logger.error("Failed to fetch the Bravo Supermarket careers page.")
            return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

        soup = BeautifulSoup(response, 'html.parser')
        job_list = soup.find('div', {'class': 'vacancies_grid'}).find_all('article')

        for job in job_list:
            job_title = job.find('h3').text.strip()
            location = job.find('footer').find('p').text.strip()
            apply_link = "https://www.bravosupermarket.az" + job.find('a')['href']

            job_data.append({
                'company': 'Azerbaijan Supermarket',
                'vacancy': job_title,
                'apply_link': apply_link
            })

        df = pd.DataFrame(job_data)
        logger.info("Scraping completed for Bravo Supermarket")
        return df if not df.empty else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

    @scraper_error_handler
    async def scrape_mdm(self, session):
        base_url = "https://www.mdm.gov.az/karyera"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

        job_data = []
        response = await self.fetch_url_async(base_url, session, headers=headers, verify_ssl=False)
        if not response:
            logger.error("Failed to fetch the Milli Depozit Mərkəzi careers page.")
            return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

        soup = BeautifulSoup(response, 'html.parser')
        content = soup.find('div', {'class': 'content'})
        paragraphs = content.find_all('p')

        job_title = None
        job_description = ""

        for p in paragraphs:
            text = p.get_text().strip()
            if text.startswith("Vəzifə :") or text.startswith("Vəzifə:"):
                if job_title:
                    job_data.append({
                        'company': 'Milli Depozit Mərkəzi',
                        'vacancy': job_title.strip(),
                        'apply_link': base_url
                    })
                job_title = text.replace("Vəzifə :", "").replace("Vəzifə:", "").strip()
                job_description = ""
            elif text.startswith("Əsas tələblər:") or text.startswith("Vəzifə və öhdəliklər:"):
                job_description += " " + text
            else:
                job_description += " " + text

        if job_title:
            job_data.append({
                'company': 'Milli Depozit Mərkəzi',
                'vacancy': job_title.strip(),
                'apply_link': base_url
            })

        df = pd.DataFrame(job_data)
        logger.info("Scraping completed for Milli Depozit Mərkəzi")
        return df if not df.empty else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

    @scraper_error_handler
    async def scrape_arti(self, session):
        logger.info("Scraping started for ARTI")
        base_url = "https://arti.edu.az/media/vakansiyalar"
        pages = 5
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

        job_data = []

        for page in range(1, pages + 1):
            url = f"{base_url}/page/{page}/"
            response = await self.fetch_url_async(url, session, headers=headers)
            if not response:
                logger.error(f"Failed to fetch page {page} for ARTI.")
                continue

            soup = BeautifulSoup(response, 'html.parser')
            cards = soup.find_all('a', {'class': 'card card-bordered card-transition h-100'})

            for card in cards:
                job_title = card.find('h4', {'class': 'card-title'}).get_text(strip=True)
                job_link = card['href']
                job_description = card.find('p', {'class': 'card-text text-body'}).get_text(strip=True)
                job_data.append({
                    'company':'Azərbaycan Respublikasının Təhsil İnstitutu',
                    'vacancy': job_title,
                    'apply_link': job_link
                })

        logger.info("Scraping completed for ARTI")
        return pd.DataFrame(job_data) if job_data else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

    @scraper_error_handler
    async def scrape_ziraat(self, session):
        base_url = 'https://ziraatbank.az'
        url = 'https://ziraatbank.az/az/vacancies2'
        
        response = await self.fetch_url_async(url, session)
        if not response:
            logger.error(f"Failed to fetch the page for Ziraat Bank.")
            return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

        soup = BeautifulSoup(response, 'html.parser')
        jobs = []

        # Find all job listings
        job_cards = soup.find_all('div', class_='landing-item-box')

        for card in job_cards:
            title_tag = card.find('h2')
            title = title_tag.get_text(strip=True) if title_tag else 'N/A'

            link_tag = card.find('a')
            link = link_tag['href'] if link_tag else '#'
            
            # Encode the link correctly
            encoded_link = quote(link, safe='/:%')
            full_link = urljoin(base_url, encoded_link)
            
            jobs.append({
                'company': 'Ziraat Bank',
                'vacancy': title,
                'apply_link': full_link
            })

        return pd.DataFrame(jobs) if jobs else pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])
    
    @scraper_error_handler
    async def scrape_staffy(self, session):
        async def fetch_jobs(page=1):
            url = "https://api.staffy.az/graphql"
            headers = {
                "Content-Type": "application/json",
                "Accept": "*/*",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "Origin": "https://staffy.az",
                "Sec-Fetch-Site": "same-site",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
                "Sec-Ch-Ua": "\"Not/A)Brand\";v=\"8\", \"Chromium\";v=\"126\", \"Google Chrome\";v=\"126\"",
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": "\"macOS\""
            }

            query = f"""
            {{
            jobs(page: {page}) {{
                totalCount
                pageInfo {{
                hasNextPage
                hasPreviousPage
                page
                totalPages
                }}
                edges {{
                node {{
                    id
                    slug
                    title
                    createdAt
                    publishedAt
                    expiresAt
                    viewCount
                    salary {{
                    from
                    to
                    }}
                    company {{
                    id
                    name
                    verified
                    }}
                }}
                }}
            }}
            }}
            """

            payload = {"query": query}

            try:
                async with session.post(url, headers=headers, json=payload) as response:
                    response.raise_for_status()
                    return await response.json()
            except aiohttp.ClientError as e:
                logger.error(f"Failed to fetch jobs: {e}")
                return None

        async def save_jobs_to_dataframe(jobs_data_list):
            all_jobs = []
            for jobs_data in jobs_data_list:
                if jobs_data and 'data' in jobs_data and 'jobs' in jobs_data['data']:
                    jobs = jobs_data['data']['jobs']['edges']
                    for job in jobs:
                        job_node = job['node']
                        job_info = {
                            "vacancy": job_node['title'],
                            "company": job_node['company']['name'],
                            "verified": job_node['company']['verified'],
                            "salary_from": job_node['salary']['from'] if job_node['salary'] else None,
                            "salary_to": job_node['salary']['to'] if job_node['salary'] else None,
                            "created_at": job_node['createdAt'],
                            "published_at": job_node['publishedAt'],
                            "expires_at": job_node['expiresAt'],
                            "view_count": job_node['viewCount'],
                            "job_id": job_node['id'],
                            "job_slug": job_node['slug'],
                            "apply_link": f"https://staffy.az/job/{job_node['slug']}"
                        }
                        all_jobs.append(job_info)

            df = pd.DataFrame(all_jobs)
            return df

        # Fetch and display job listings with pagination
        page = 1
        max_pages = 8  # Set limit for the number of pages to fetch
        all_jobs_data = []

        while page <= max_pages:
            jobs_data = await fetch_jobs(page)
            if jobs_data:
                all_jobs_data.append(jobs_data)
                if not jobs_data['data']['jobs']['pageInfo']['hasNextPage']:
                    break
                page += 1
            else:
                break

        # Save all fetched job data to a DataFrame
        jobs_df = await save_jobs_to_dataframe(all_jobs_data)

        # Return only the specific columns with renamed columns
        result_df = jobs_df[['company', 'vacancy', 'apply_link']]
        return result_df
    
    @scraper_error_handler
    async def scrape_position_az(self, session):
        url = 'https://position.az'

        async with session.get(url) as response:
            if response.status == 200:
                # Parse the HTML content using BeautifulSoup
                content = await response.text()
                soup = BeautifulSoup(content, 'html.parser')
                
                # Find the job listings
                job_listings = soup.find_all('tr', {'class': lambda x: x and x.startswith('category-')})
                
                # Initialize lists to store the job data
                vacancies = []
                companies = []
                apply_links = []
                
                # Loop through each job listing and extract the data
                for job in job_listings:
                    vacancy = job.find('td', {'title': True}).get_text(strip=True)
                    company = job.find_all('td')[1].get_text(strip=True)
                    apply_link = job.find('a')['href']
                    
                    vacancies.append(vacancy)
                    companies.append(company)
                    # Fix the apply link if it does not start with 'https://position.az'
                    if not apply_link.startswith('https://position.az'):
                        apply_link = url + apply_link
                    apply_links.append(apply_link)
                
                # Create a DataFrame from the lists
                data = {
                    'vacancy': vacancies,
                    'company': companies,
                    'apply_link': apply_links
                }
                df = pd.DataFrame(data)
                
                return df
            else:
                logger.error(f"Failed to retrieve the webpage. Status code: {response.status}")
                return pd.DataFrame(columns=['vacancy', 'company', 'apply_link'])

    @scraper_error_handler
    async def scrape_hrin_co(self, session):
        base_url = 'https://hrin.co/?page={}'
        job_listings = []

        for page in range(1, 6):  # Scraping pages 1 to 5
            url = base_url.format(page)
            async with session.get(url) as response:
                if response.status == 200:
                    content = await response.text()
                    soup = BeautifulSoup(content, 'html.parser')
                    job_cards = soup.find_all('div', class_='vacancy-list-item')
                    
                    for job in job_cards:
                        company_tag = job.find('a', class_='company')
                        vacancy_tag = job.find('a', class_='title')
                        
                        company = company_tag.get_text(strip=True) if company_tag else 'N/A'
                        vacancy = vacancy_tag.get_text(strip=True) if vacancy_tag else 'N/A'
                        apply_link = vacancy_tag['href'] if vacancy_tag else 'N/A'
                        
                        job_listings.append({
                            'company': company,
                            'vacancy': vacancy,
                            'apply_link': apply_link
                        })
                else:
                    logger.error(f"Failed to retrieve the webpage for page {page}. Status code: {response.status}")

        df = pd.DataFrame(job_listings)
        return df

    @scraper_error_handler
    async def scrape_un_jobs(self, session):
        logger.info("Scraping started for UN")
        url = 'https://azerbaijan.un.org/az/jobs'
        base_url = 'https://azerbaijan.un.org'

        async with session.get(url) as response:
            if response.status == 200:
                content = await response.text()
                soup = BeautifulSoup(content, 'html.parser')
                job_listings = []

                # Find all job article elements
                job_cards = soup.find_all('article', class_='node--view-mode-teaser')

                for job in job_cards:
                    # Extract the title and link
                    title_tag = job.find('a', attrs={'data-once': 'submenu-reveal'})
                    title = title_tag.get_text(strip=True) if title_tag else 'N/A'  # Fall back to get_text() if title attribute is missing
                    href = title_tag['href'] if title_tag else ''
                    
                    # Ensure the full apply link is constructed correctly
                    if href.startswith('http'):
                        apply_link = href
                    else:
                        apply_link = urljoin(base_url, href)

                    # Extract the organization name
                    organization_tag = job.find('div', class_='text-un-gray-dark text-lg')
                    organization = organization_tag.get_text(strip=True) if organization_tag else 'N/A'

                    job_listings.append({
                        'company': organization,
                        'vacancy': title,
                        'apply_link': apply_link
                    })

                df = pd.DataFrame(job_listings)
                logger.info("Scraping completed for UN")
                logger.info(f"\n{df}")
                return df
            else:
                logger.error(f"Failed to retrieve the webpage. Status code: {response.status}")
                return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

    @scraper_error_handler
    async def scrape_oilfund_jobs(self, session):
        url = 'https://oilfund.az/fund/career-opportunities/vacancy'
        async with session.get(url, verify_ssl=False) as response:
            if response.status == 200:
                content = await response.text()
                soup = BeautifulSoup(content, 'html.parser')
                job_listings = []
                
                job_cards = soup.find_all('div', class_='oil-q-box')
                
                for job in job_cards:
                    title_tag = job.find('a', class_='font-gotham-book')
                    if title_tag:
                        title = title_tag.get_text(strip=True)
                        apply_link = title_tag['href']
                        job_listings.append({
                            'company':'Azərbaycan Respublikasının Dövlət Neft Fondu',
                            'vacancy': title,
                            'apply_link': apply_link
                        })
                
                df = pd.DataFrame(job_listings)
                return df
            else:
                logger.error(f"Failed to retrieve the webpage. Status code: {response.status}")
                return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

    @scraper_error_handler
    async def scrape_1is_az(self, session):
        logger.info('Scraping started for 1is.az')
        pages = 3
        base_url = "https://1is.az/vsearch?expired=on&sort_by=1&page="
        job_listings = []

        for page in range(1, pages + 1):
            url = base_url + str(page)
            async with session.get(url) as response:
                if response.status != 200:
                    logger.error(f"Failed to fetch page {page}. Status code: {response.status}")
                    continue

                html_content = await response.text()
                soup = BeautifulSoup(html_content, "html.parser")

                all_vacancies = soup.find_all('div', class_='vac-card')
                for vac in all_vacancies:
                    job = {}

                    vac_inner1 = vac.find('div', class_='vac-inner1')
                    if vac_inner1:
                        category = vac_inner1.find('a', class_='vac-inner1-a')
                        if category:
                            job['category'] = category.text.strip()
                        
                        views = vac_inner1.find('span', class_='look-numb')
                        if views:
                            job['views'] = views.text.strip()

                    vac_inner2 = vac.find('div', class_='vac-inner2')
                    if vac_inner2:
                        job_title = vac_inner2.find('a', class_='vac-name')
                        if job_title:
                            job['vacancy'] = job_title.text.strip()
                            job['apply_link'] = job_title['href']

                    vac_inner3 = vac.find('div', class_='vac-inner3')
                    if vac_inner3:
                        company_info = vac_inner3.find('div', class_='vac-inn1')
                        if company_info:
                            company = company_info.find('a', class_='comp-link')
                            if company:
                                job['company'] = company.text.strip()
                                job['company_link'] = company['href']

                    if 'company' in job and 'vacancy' in job and 'apply_link' in job:
                        job_listings.append(job)
        
        logger.info("Scraping completed for 1is.az")
        
        return pd.DataFrame(job_listings, columns=['company', 'vacancy', 'apply_link'])

    @scraper_error_handler
    async def scrape_themuse_api(self, session):
        api_url = "https://www.themuse.com/api/search-renderer/jobs"
        params = {
            'ctsEnabled': 'false',
            'latlng': '40.37767028808594,49.89200973510742',
            'preference': 'bf2kq0pm0q8',
            'limit': 100,
            'query': '',
            'timeout': 5000
        }

        async with session.get(api_url, params=params) as response:
            if response.status != 200:
                logger.error(f"Failed to fetch data. Status code: {response.status}")
                return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

            data = await response.json()

            jobs = []
            for hit in data.get('hits', []):
                job_data = hit.get('hit', {})
                company_name = job_data.get('company', {}).get('name', '')
                vacancy_title = job_data.get('title', '')
                company_short_name = job_data.get('company', {}).get('short_name', '')
                short_title = job_data.get('short_title', '')
                apply_link = f"https://www.themuse.com/jobs/{company_short_name}/{short_title}"

                job = {
                    'company': company_name,
                    'vacancy': vacancy_title,
                    'apply_link': apply_link
                }
                jobs.append(job)

        return pd.DataFrame(jobs, columns=['company', 'vacancy', 'apply_link'])

    @scraper_error_handler
    async def scrape_dejobs(self, session):
        url = "https://dejobs.org/aze/jobs/#1"
        async with session.get(url) as response:
            if response.status != 200:
                logger.error(f"Failed to fetch data. Status code: {response.status}")
                return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

            soup = BeautifulSoup(await response.text(), 'html.parser')

            jobs = []
            job_listings = soup.find_all('li', class_='direct_joblisting')

            for job in job_listings:
                try:
                    vacancy = job.find('span', class_='resultHeader').text.strip()
                    apply_link = "https://dejobs.org" + job.find('a')['href'].strip()
                    company = job.find('b', class_='job-location-information').text.strip()

                    jobs.append({
                        'company': company,
                        'vacancy': vacancy,
                        'apply_link': apply_link
                    })
                except AttributeError as e:
                    logger.warning(f"Error parsing job: {e}")
                    continue

            return pd.DataFrame(jobs, columns=['company', 'vacancy', 'apply_link'])

    @scraper_error_handler
    async def scrape_hcb(self, session):
        url = 'https://hcb.az/'
        async with session.get(url) as response:
            response.raise_for_status()
            soup = BeautifulSoup(await response.text(), 'html.parser')
            
            jobs = []
            table_rows = soup.select('.table-bg table tbody tr')
            for row in table_rows:
                columns = row.find_all('td')
                if len(columns) >= 6:
                    apply_link = columns[0].find('a')['href']
                    vacancy = columns[2].get_text(strip=True)
                    company = columns[3].get_text(strip=True)

                    jobs.append({
                        'company': company,
                        'vacancy': vacancy,
                        'apply_link': apply_link
                    })

            return pd.DataFrame(jobs)
        
    @scraper_error_handler
    async def scrape_bfb(self, session):
        url = "https://www.bfb.az/en/careers"
        async with session.get(url) as response:
            soup = BeautifulSoup(await response.text(), "html.parser")

            titles = []
            job_listings = soup.select("ul.page-list > li")

            for listing in job_listings:
                title_tag = listing.find("h3", class_="accordion-title")
                title = title_tag.get_text(strip=True) if title_tag else "N/A"
                titles.append(title)

            return pd.DataFrame({
                'company': 'Baku Stock Exchange',
                "vacancy": titles,
                "apply_link" : 'https://www.bfb.az/en/careers',
            })

    @scraper_error_handler
    async def scrape_airswift(self, session):
        url = "https://www.airswift.com/jobs?search=&location=Baku&verticals_discipline=*&sector=*&employment_type=*&date_published=*"
        async with session.get(url) as response:
            soup = BeautifulSoup(await response.text(), "html.parser")

            titles = []
            apply_links = []

            job_cards = soup.select("div.jobs__card")

            for card in job_cards:
                title_tag = card.select_one("div.title")
                title = title_tag.get_text(strip=True) if title_tag else "N/A"
                titles.append(title)

                apply_link_tag = card.select_one("a.c-button.candidate-conversion-apply")
                apply_link = apply_link_tag["href"] if apply_link_tag else "N/A"
                apply_links.append(apply_link)

            return pd.DataFrame({
                'company': 'Unknown',
                'vacancy': titles,
                "apply_link": apply_links
            })
            
    @scraper_error_handler
    async def scrape_orion(self, session):
        async def get_orion_jobs(page):
            url = f"https://www.orionjobs.com/jobs/azerbaijan-office?page={page}"
            async with session.get(url) as response:
                if response.status != 200:
                    logger.warning(f"Failed to retrieve page {page}")
                    return []

                soup = BeautifulSoup(await response.text(), "html.parser")
                job_list = []

                for job in soup.select("ul.results-list li.job-result-item"):
                    title = job.select_one(".job-title a").get_text(strip=True)
                    apply_url = job.select_one(".job-apply-now-link a")["href"]

                    job_list.append({
                        "company": "Unknown",
                        "vacancy": title,
                        "apply_link": f"https://www.orionjobs.com{apply_url}"
                    })

                return job_list

        all_jobs = []
        for page in range(1, 6):  # Scrape pages 1 to 5
            jobs = await get_orion_jobs(page)
            if jobs:
                all_jobs.extend(jobs)
            else:
                logger.info(f"No jobs found on page {page}")

        if all_jobs:
            return pd.DataFrame(all_jobs)
        else:
            logger.info("No jobs data to save")
            return pd.DataFrame()
        
    @scraper_error_handler
    async def scrape_hrcbaku(self, session):
        url = "https://hrcbaku.com/jobs-1"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

        async with session.get(url, headers=headers) as response:
            if response.status != 200:
                logger.error(f"Failed to retrieve the page. Status code: {response.status}")
                return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

            soup = BeautifulSoup(await response.text(), "html.parser")
            
            jobs = []
            job_containers = soup.find_all("div", class_="tn-elem")

            for job_container in job_containers:
                title_elem = job_container.find("a", href=True)
                if title_elem and "vacancy" in title_elem['href']:
                    title = title_elem.get_text(strip=True)
                    apply_link = "https://hrcbaku.com" + title_elem['href']
                    description = title_elem.get_text(strip=True)
                    location = "Baku, Azerbaijan"

                    # Finding the company and any adjacent information if available
                    company = "Not specified"
                    company_elem = job_container.find_previous_sibling("dxiv", class_="tn-elem")
                    if company_elem:
                        company_text = company_elem.get_text(strip=True)
                        if company_text and "Apply" not in company_text:
                            company = company_text

                    if "Apply" not in title:
                        jobs.append({
                            "company": company,
                            "vacancy": title,
                            "apply_link": apply_link
                        })
            
            return pd.DataFrame(jobs)
 
    @scraper_error_handler
    async def parse_jobsearch_az(self, session):
        """Fetch job data from Jobsearch.az and return a DataFrame."""
        # Initial request to obtain cookies
        initial_url = "https://www.jobsearch.az"
        
        # Perform an initial request to the homepage to set up cookies in the session
        async with session.get(initial_url) as initial_response:
            if initial_response.status != 200:
                logger.error(f"Failed to obtain initial cookies: {initial_response.status}")
                return pd.DataFrame(columns=['vacancy', 'company', 'apply_link'])
            
            # Session cookies are now set and can be used in subsequent requests

        # Base URL for the API request
        base_url = "https://www.jobsearch.az/api-az/vacancies-az"
        params = {
            'hl': 'az',
            'q': '',
            'posted_date': '',
            'seniority': '',
            'categories': '',
            'industries': '',
            'ads': '',
            'location': '',
            'job_type': '',
            'salary': '',
            'order_by': ''
        }

        # Headers for the request
        headers = {
            'authority': 'www.jobsearch.az',
            'accept': 'application/json, text/plain, */*',
            'accept-encoding': 'gzip, deflate, br, zstd',
            'accept-language': 'en-GB,en-US;q=0.9,en;q=0.8,ru;q=0.7,az;q=0.6',
            'dnt': '1',
            'priority': 'u=1, i',
            'referer': 'https://www.jobsearch.az/vacancies',
            'sec-ch-ua': '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"macOS"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
            'x-requested-with': 'XMLHttpRequest'
        }

        # List to hold job data
        job_listings = []

        # Initialize page counter
        page_count = 0

        # Loop to fetch and process up to 5 pages
        while page_count < 5:
            async with session.get(base_url, params=params, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()

                    # Process each job in the current page
                    for job in data.get('items', []):
                        job_listings.append({
                            "vacancy": job['title'],
                            "company": job['company']['title'],
                            "apply_link": f"https://www.jobsearch.az/vacancies/{job['slug']}"
                        })

                    # Check if there is a next page URL
                    if 'next' in data:
                        next_page_url = data['next']
                        base_url = next_page_url
                        params = {}  # Reset params since the next page URL includes all parameters
                        page_count += 1  # Increment page counter
                    else:
                        break  # No more pages, exit the loop
                else:
                    logger.error(f"Failed to retrieve data: {response.status}")
                    break

        # Convert the list of jobs to a DataFrame
        df = pd.DataFrame(job_listings, columns=['vacancy', 'company', 'apply_link'])
        return df

    # @scraper_error_handler
    # async def scrape_canscreen(self, session):
    #     """
    #     Scrape vacancies from the CanScreen API and return the data as a DataFrame
    #     with columns 'company', 'vacancy', and 'apply_link'.
        
    #     Args:
    #         session (aiohttp.ClientSession): The aiohttp session object for making HTTP requests.
        
    #     Returns:
    #         pd.DataFrame: A DataFrame containing the scraped vacancy data with specific columns.
    #     """
    #     api_url = "https://canscreen.io/_next/data/W5jP3jS8JZCd25SRiR4oo/en/vacancies.json"
        
    #     try:
    #         async with session.get(api_url) as response:
    #             if response.status == 200:
    #                 data = await response.json()
    #                 vacancies = data['pageProps']['vacancies']

    #                 jobs = []

    #                 for vacancy in vacancies:
    #                     title = vacancy['title']
    #                     company = vacancy['company']
    #                     apply_link = f"https://canscreen.io/vacancies/{vacancy['id']}/"

    #                     jobs.append({
    #                         'company': company,
    #                         'vacancy': title,
    #                         'apply_link': apply_link
    #                     })

    #                 df = pd.DataFrame(jobs)
    #                 return df
    #             else:
    #                 logger.error(f"Failed to fetch data from the API. Status code: {response.status}")
    #                 return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])  # Return empty DataFrame on failure

    #     except Exception as e:
    #         logger.error(f"An error occurred: {e}")
    #         return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])  # Return empty DataFrame on exception

    @scraper_error_handler
    async def scrape_canscreen(self, session):
        """
        Scrape vacancies from the CanScreen API with dynamic build ID handling.
        """
        # First, fetch the main page to get the current build ID
        base_url = "https://canscreen.io"
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br'
        }
        
        try:
            # Get the main page first
            async with session.get(f"{base_url}/en/vacancies", headers=headers) as response:
                if response.status != 200:
                    logger.error(f"Failed to fetch main page. Status code: {response.status}")
                    return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])
                
                html_content = await response.text()
                
                # Extract the build ID from the page
                # Look for the script containing __NEXT_DATA__
                import re
                build_id_match = re.search(r'"buildId":"([^"]+)"', html_content)
                
                if not build_id_match:
                    logger.error("Could not find build ID in the page")
                    return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])
                    
                build_id = build_id_match.group(1)
                
                # Now construct the API URL with the current build ID
                api_url = f"{base_url}/_next/data/{build_id}/en/vacancies.json"
                
                # Fetch the actual vacancy data
                async with session.get(api_url, headers=headers) as api_response:
                    if api_response.status == 200:
                        data = await api_response.json()
                        vacancies = data.get('pageProps', {}).get('vacancies', [])
                        
                        jobs = []
                        for vacancy in vacancies:
                            jobs.append({
                                'company': vacancy.get('company', 'Unknown'),
                                'vacancy': vacancy.get('title', 'Unknown'),
                                'apply_link': f"{base_url}/vacancies/{vacancy.get('id')}/"
                            })
                        
                        return pd.DataFrame(jobs)
                    else:
                        logger.error(f"Failed to fetch vacancy data. Status: {api_response.status}")
                        return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])
                        
        except Exception as e:
            logger.error(f"Error in CanScreen scraper: {str(e)}")
            return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])

    @scraper_error_handler
    async def parse_azercosmos(self, session):
        """
        Scrape job vacancies from Azercosmos careers page with enhanced parsing
        """
        logger.info("Started scraping Azercosmos")
        url = "https://azercosmos.az/en/about-us/careers"
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1'
        }
        
        try:
            response = await self.fetch_url_async(url, session, headers=headers)
            
            if not response:
                logger.error("Failed to retrieve the Azercosmos careers page")
                return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])
            
            soup = BeautifulSoup(response, 'html.parser')
            job_listings = []
            
            # First, find the section containing careers
            careers_section = soup.find('section', class_='careers')
            if not careers_section:
                logger.warning("Careers section not found")
                return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])
            
            # Find the positions container
            positions_div = careers_section.find('div', class_='positions')
            if not positions_div:
                logger.warning("Positions container not found")
                return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])
            
            # Find all collapsible job divs within the positions container
            collapsibles = positions_div.find_all('div', class_='collapsible')
            base_url = "https://azercosmos.az"
            
            for collapsible in collapsibles:
                try:
                    # Find the label containing the job title
                    label = collapsible.find('label')
                    if not label:
                        continue
                        
                    # Find the title span within the flex container
                    flex_container = label.find('div', class_='flex')
                    if not flex_container:
                        continue
                        
                    title_span = flex_container.find('span')
                    title = title_span.text.strip() if title_span else None
                    
                    # Find the collapser div which contains the apply link
                    collapser = collapsible.find('div', class_='collapser')
                    if not collapser:
                        continue
                        
                    # Find the apply link
                    apply_link_elem = collapser.find('a', href=True)
                    apply_link = urljoin(base_url, apply_link_elem['href']) if apply_link_elem else None
                    
                    if title and apply_link:
                        job_listings.append({
                            'company': 'Azercosmos',
                            'vacancy': title,
                            'apply_link': apply_link
                        })
                except Exception as e:
                    logger.warning(f"Error parsing job listing: {str(e)}")
                    continue
            
            logger.info(f"Successfully scraped {len(job_listings)} jobs from Azercosmos")
            return pd.DataFrame(job_listings)
        
        except Exception as e:
            logger.error(f"Error scraping Azercosmos: {str(e)}")
            return pd.DataFrame(columns=['company', 'vacancy', 'apply_link'])