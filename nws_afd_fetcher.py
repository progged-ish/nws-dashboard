#!/usr/bin/env python3
"""Fetch NWS Area Forecast Discussions with logging and error handling."""

import requests
import logging
from datetime import datetime
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/var/log/nws_dashboard/fetcher.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Configuration
NWS_API_BASE = "https://api.weather.gov"
TWC_AFD_ENDPOINT = f"{NWS_API_BASE}/products/types/AFD/locations/TWC"


def fetch_tucson_afd() -> str:
    """Fetch the latest Tucson AFD from NWS API.
    
    Returns:
        The full product text of the most recent AFD.
        
    Raises:
        ValueError: If no products found or required fields missing
        requests.RequestException: If API calls fail
    """
    try:
        # Step 1: Get list of recent AFDs for Tucson (TWC)
        logger.info(f"Fetching AFD list from {TWC_AFD_ENDPOINT}")
        response = requests.get(TWC_AFD_ENDPOINT, headers={"Accept": "application/json"})
        response.raise_for_status()
        products_list = response.json()

        if not products_list or 'features' not in products_list:
            raise ValueError("No AFD products found")

        # Step 2: Grab the @id of the first product
        first_product = products_list['features'][0]
        first_product_id = first_product.get('@id')
        if not first_product_id:
            raise ValueError("Could not find @id in first product")

        logger.info(f"Found AFD ID: {first_product_id}")

        # Step 3: Fetch the full product text
        logger.info(f"Fetching full product from {first_product_id}")
        product_response = requests.get(first_product_id, headers={"Accept": "application/json"})
        product_response.raise_for_status()
        product_data = product_response.json()

        afd_text = product_data.get('productText')
        if not afd_text:
            raise ValueError("No productText in response")

        logger.info("Successfully fetched Tucson AFD")
        return afd_text.strip()

    except requests.RequestException as e:
        logger.error(f"API request failed: {e}")
        raise
    except KeyError as e:
        logger.error(f"Unexpected data structure: missing key {e}")
        raise


if __name__ == "__main__":
    try:
        text = fetch_tucson_afd()
        print("\n" + "="*80)
        print(text)
        print("="*80)
    except Exception as e:
        logger.error(f"Failed to fetch AFD: {e}")
        exit(1)
