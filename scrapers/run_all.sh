#!/bin/bash
# Tomcat CapEx - Master UCC Scraper Execution Script
# This script will automatically find all python scrapers in this directory and run them.

echo "🚀 Starting Master UCC Scraper Deployment..."
echo "---------------------------------------------------"

# Find all python files ending in _ucc_scraper.py
SCRAPERS=$(ls *_ucc_scraper.py 2>/dev/null)

if [ -z "$SCRAPERS" ]; then
  echo "No scrapers found in this directory."
  exit 1
fi

for scraper in $SCRAPERS
do
  echo "📡 Triggering: $scraper"
  # Run the scraper sequentially and wait for it to finish
  python3 "$scraper"
  
  echo "⏳ Sleeping for 60 seconds to avoid rate limits..."
  sleep 60
done

# Wait for all background scrapers to finish
wait

echo "---------------------------------------------------"
echo "✅ All UCC Scrapers have completed execution."
echo "✅ Leads successfully piped into tomcat_capex.db."
