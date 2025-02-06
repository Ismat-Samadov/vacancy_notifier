# main.py
import sys
import logging
import asyncio
import aiohttp
import aiosmtplib
from email_notifier import EmailNotifier
from job_scraper import JobScraper

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(name)s:%(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

async def main():
    try:
        # Configure longer timeout for aiohttp
        timeout = aiohttp.ClientTimeout(total=300)  # 5 minutes
        
        connector = aiohttp.TCPConnector(
            limit=10,  # Reduced concurrent connections
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
            force_close=True,
            ssl=False  # Disable SSL verification for problematic sites
        )

        # Create custom session with retry logic
        async with aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            raise_for_status=False
        ) as session:
            job_scraper = JobScraper()
            await job_scraper.get_data_async()
            
            if job_scraper.data is not None and not job_scraper.data.empty:
                # Properly await the save_to_db operation
                # await job_scraper.save_to_db(job_scraper.data)
                
                # Configure email notifier with error handling
                try:
                    email_notifier = EmailNotifier()
                    await email_notifier.send_notification(job_scraper.data)
                except aiosmtplib.errors.SMTPException as e:
                    logger.error(f"SMTP error: {str(e)}")
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
