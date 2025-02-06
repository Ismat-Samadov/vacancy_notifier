import sys
import logging
import asyncio
import aiohttp
import aiosmtplib
from email_notifier import EmailNotifier
from job_scraper import JobScraper
from typing import Optional
import json

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(name)s:%(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

async def safe_json_parse(text: str) -> Optional[dict]:
    """Safely parse JSON with proper error handling."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"JSON parsing error: {str(e)}")
        return None

class RetryableHTTPClient:
    def __init__(self, session: aiohttp.ClientSession, max_retries: int = 3, base_delay: float = 1.0):
        self.session = session
        self.max_retries = max_retries
        self.base_delay = base_delay

    async def request(self, method: str, url: str, **kwargs) -> Optional[aiohttp.ClientResponse]:
        for attempt in range(self.max_retries):
            try:
                timeout = aiohttp.ClientTimeout(total=30)  # Reduced timeout
                kwargs['timeout'] = timeout
                
                async with self.session.request(method, url, **kwargs) as response:
                    if response.status == 200:
                        return response
                    elif response.status in [429, 503]:  # Rate limiting or service unavailable
                        delay = self.base_delay * (2 ** attempt)
                        logger.warning(f"Rate limited or service unavailable. Retrying in {delay}s...")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        logger.error(f"HTTP {response.status} error for {url}")
                        return None
                        
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                delay = self.base_delay * (2 ** attempt)
                logger.error(f"Connection error (attempt {attempt + 1}): {str(e)}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(delay)
                    continue
                return None
                
        return None

async def main():
    try:
        # Configure session with more conservative timeouts and limits
        timeout = aiohttp.ClientTimeout(
            total=180,        # 3 minutes total
            connect=30,       # 30 seconds connection timeout
            sock_connect=30,  # 30 seconds socket connection timeout
            sock_read=30      # 30 seconds socket read timeout
        )
        
        connector = aiohttp.TCPConnector(
            limit=5,           # Very conservative connection limit
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
            force_close=True,
            ssl=False         # Disable SSL verification for problematic sites
        )

        # Create session with retry logic
        async with aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            raise_for_status=False
        ) as session:
            # Initialize HTTP client with retry logic
            http_client = RetryableHTTPClient(session)
            
            # Initialize job scraper with the HTTP client
            job_scraper = JobScraper()
            job_scraper.http_client = http_client
            
            # Attempt to fetch and process job data
            await job_scraper.get_data_async()
            
            if job_scraper.data is not None and not job_scraper.data.empty:
                try:
                    # Initialize email notifier with proper error handling
                    email_notifier = EmailNotifier()
                    
                    # Validate SMTP settings before sending
                    smtp_settings = {
                        'hostname': email_notifier.smtp_server.strip(),  # Remove any whitespace
                        'port': email_notifier.smtp_port,
                        'username': email_notifier.smtp_username,
                        'password': email_notifier.smtp_password
                    }
                    
                    # Validate SMTP hostname
                    if '\n' in smtp_settings['hostname'] or '\r' in smtp_settings['hostname']:
                        raise ValueError("SMTP hostname contains invalid characters")
                    
                    # Send notifications with validated settings
                    await email_notifier.send_notification(job_scraper.data)
                    
                except aiosmtplib.errors.SMTPException as e:
                    logger.error(f"SMTP error: {str(e)}")
                except ValueError as e:
                    logger.error(f"Configuration error: {str(e)}")
                except Exception as e:
                    logger.error(f"Email notification error: {str(e)}")
            else:
                logger.warning("No job data available to process")
                
    except KeyboardInterrupt:
        logger.info("Process interrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Error in main execution: {str(e)}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())