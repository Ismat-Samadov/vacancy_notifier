# main.py
import sys
import logging
import asyncio
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
        job_scraper = JobScraper()
        await job_scraper.get_data_async()
        
        if job_scraper.data is not None and not job_scraper.data.empty:
            # Save to database - now properly awaited
            await job_scraper.save_to_db(job_scraper.data)
            
            email_notifier = EmailNotifier()
            await email_notifier.send_notification(job_scraper.data)
            
    except KeyboardInterrupt:
        logger.info("Process interrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Error in main execution: {str(e)}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
