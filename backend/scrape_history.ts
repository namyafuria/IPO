import axios from 'axios';
import * as cheerio from 'cheerio';
import { getDb } from './db';

const YEARS = [2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026];

async function delay(ms: number) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function scrapeYear(year: number) {
  console.log(`Scraping data for year ${year}...`);
  const urlsToTry = [
    `https://www.chittorgarh.com/ipo_perf_tracker.asp?exchange=mainline&year=${year}`,
    `https://www.chittorgarh.com/report/ipo-in-india-list-main-board-sme/82/all/?year=${year}`
  ];

  let ipos: any[] = [];
  
  for (const url of urlsToTry) {
    try {
      const response = await axios.get(url, {
        headers: {
          'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        },
        timeout: 10000
      });
      
      const $ = cheerio.load(response.data);
      
      $('table').each((i, table) => {
        const headers: string[] = [];
        $(table).find('th').each((j, th) => {
          headers.push($(th).text().trim().toLowerCase());
        });
        
        let nameIdx = headers.findIndex(h => h.includes('ipo name') || h.includes('company') || h.includes('issuer'));
        let dateIdx = headers.findIndex(h => h.includes('listing date') || h.includes('date'));
        let gainIdx = headers.findIndex(h => h.includes('gain') || h.includes('listing gain'));
        let issuePriceIdx = headers.findIndex(h => h.includes('issue price') || h.includes('price'));
        let issueSizeIdx = headers.findIndex(h => h.includes('issue size') || h.includes('size'));
        
        if (nameIdx === -1) nameIdx = 0; // Assume first column is name
        
        $(table).find('tbody tr, tr').each((j, tr) => {
          if ($(tr).find('th').length > 0) return; // skip header rows
          
          const cols = $(tr).find('td');
          if (cols.length > nameIdx) {
            let name = $(cols[nameIdx]).text().trim();
            name = name.replace(/IPO|Ltd|Limited/gi, '').trim();
            const dateStr = dateIdx !== -1 && $(cols[dateIdx]) ? $(cols[dateIdx]).text().trim() : '';
            const gainStr = gainIdx !== -1 && $(cols[gainIdx]) ? $(cols[gainIdx]).text().trim() : '';
            const priceStr = issuePriceIdx !== -1 && $(cols[issuePriceIdx]) ? $(cols[issuePriceIdx]).text().trim() : '';
            const sizeStr = issueSizeIdx !== -1 && $(cols[issueSizeIdx]) ? $(cols[issueSizeIdx]).text().trim() : '';
            
            if (name && name !== '') {
              let gain: number | null = null;
              if (gainStr) {
                const match = gainStr.match(/(-?[\d.]+)%/);
                if (match) gain = parseFloat(match[1]);
                else {
                  const num = parseFloat(gainStr.replace(/,/g, ''));
                  if (!isNaN(num)) gain = num;
                }
              }
              
              let listedDate = null;
              if (dateStr) {
                const d = new Date(dateStr);
                if (!isNaN(d.getTime())) {
                  listedDate = d.toISOString().split('T')[0];
                }
              }
              
              let issuePrice = null;
              if (priceStr) {
                const match = priceStr.match(/(\d+)/);
                if (match) issuePrice = parseFloat(match[1]);
              }

              let issueSize = null;
              if (sizeStr) {
                const match = sizeStr.match(/([\d.]+)/);
                if (match) issueSize = parseFloat(match[1]);
              }

              ipos.push({
                name,
                listed_date: listedDate || `${year}-01-01`,
                listing_gain_pct: gain,
                issue_price: issuePrice,
                issue_size_cr: issueSize,
                sector: null
              });
            }
          }
        });
      });

      if (ipos.length > 5) {
        break; // Successfully scraped this year
      }
    } catch (error: any) {
      console.error(`Error scraping URL ${url} for year ${year}:`, error.message);
    }
  }

  if (ipos.length < 5) {
    console.warn(`Only scraped ${ipos.length} IPOs for ${year} -- Chittorgarh's page layout may` +
      ` have changed, or the site may be rate-limiting/blocking this request. Leaving this year` +
      ` as-is rather than inserting fabricated placeholder rows.`);
  }

  return ipos;
}

async function run() {
  console.log('Starting historical data scraping (2016-2026)...');
  const db = getDb();
  
  const insertStmt = db.prepare(`
    INSERT INTO ipos (name, listed_date, listing_gain_pct, issue_price, issue_size_cr, sector, data_source, last_updated, gain_basis)
    VALUES (?, ?, ?, ?, ?, ?, 'Chittorgarh', datetime('now'), 'close')
    ON CONFLICT(name) DO UPDATE SET 
      listed_date = excluded.listed_date,
      listing_gain_pct = excluded.listing_gain_pct,
      issue_price = COALESCE(ipos.issue_price, excluded.issue_price),
      issue_size_cr = COALESCE(ipos.issue_size_cr, excluded.issue_size_cr)
  `);

  let totalAdded = 0;

  for (const year of YEARS) {
    const ipos = await scrapeYear(year);
    
    let yearAdded = 0;
    db.transaction(() => {
      for (const ipo of ipos) {
        insertStmt.run(ipo.name, ipo.listed_date, ipo.listing_gain_pct, ipo.issue_price, ipo.issue_size_cr, ipo.sector);
        yearAdded++;
        totalAdded++;
      }
    })();
    
    console.log(`Processed ${yearAdded} IPOs from ${year}.`);
    await delay(1000); // polite scraping
  }

  console.log(`\nScraping complete. Total ${totalAdded} IPOs processed into database.`);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  run().catch(console.error);
}
