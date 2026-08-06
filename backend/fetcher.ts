import axios from 'axios';
import * as cheerio from 'cheerio';
import { getDb } from './db';

const CHITTORGARH_MAIN = 'https://www.chittorgarh.com/report/ipo-in-india-list-main-board-sme/82/';

export async function fetchIPOListFromChittorgarh(year: number) {
  console.log(`Fetching IPO list for year ${year} from Chittorgarh...`);
  const db = getDb();
  let added = 0;
  
  try {
    const url = `https://www.chittorgarh.com/ipo_perf_tracker.asp?exchange=mainline&year=${year}`;
    const response = await axios.get(url, {
      headers: { 'User-Agent': 'Mozilla/5.0' },
      timeout: 15000
    });
    
    const $ = cheerio.load(response.data);
    
    db.transaction(() => {
      $('table').each((i, table) => {
        const headers: string[] = [];
        $(table).find('th').each((j, th) => {
          headers.push($(th).text().trim().toLowerCase());
        });
        
        if (headers.some(h => h.includes('ipo name') || h.includes('company')) && 
            headers.some(h => h.includes('list') || h.includes('gain'))) {
          
          let nameIdx = headers.findIndex(h => h.includes('ipo name') || h.includes('company'));
          let dateIdx = headers.findIndex(h => h.includes('listing date') || h.includes('date'));
          
          if (nameIdx === -1) nameIdx = 0;
          
          $(table).find('tbody tr, tr').each((j, tr) => {
            if (j === 0 && $(tr).find('th').length > 0) return;
            
            const cols = $(tr).find('td');
            if (cols.length > nameIdx) {
              const name = $(cols[nameIdx]).text().trim().replace(/IPO|Ltd|Limited/gi, '').trim();
              const dateStr = dateIdx !== -1 ? $(cols[dateIdx]).text().trim() : '';
              
              if (name && name !== '') {
                let listedDate = null;
                if (dateStr) {
                  const d = new Date(dateStr);
                  if (!isNaN(d.getTime())) {
                    listedDate = d.toISOString().split('T')[0];
                  }
                }
                
                const exists = db.prepare('SELECT 1 FROM ipos WHERE name = ?').get(name);
                if (!exists) {
                  db.prepare(`
                    INSERT INTO ipos (name, listed_date, data_source, last_updated)
                    VALUES (?, ?, 'Chittorgarh Tracker', datetime('now'))
                  `).run(name, listedDate);
                  added++;
                }
              }
            }
          });
        }
      });
    })();
  } catch (error: any) {
    console.error(`Error fetching year ${year}:`, error.message);
  }
  
  return { success: true, added };
}

export async function fetchIPODetailFromChittorgarh(ipoName: string) {
  console.log(`Fetching details from Chittorgarh for: ${ipoName}`);
  const db = getDb();
  
  let ipo = db.prepare('SELECT * FROM ipos WHERE name = ?').get(ipoName) as any;
  if (!ipo) {
    console.error(`IPO ${ipoName} not found in DB.`);
    return null;
  }

  let detailUrl = ipo.source_url;

  // If we don't have the URL, we must find it by searching the main list pages
  if (!detailUrl) {
    console.log(`No source_url for ${ipoName}, searching main lists...`);
    const searchUrls = [
      'https://www.chittorgarh.com/report/ipo-in-india-list-main-board-sme/82/',
      'https://www.chittorgarh.com/report/ipo-in-india-list-main-board-sme/82/all/'
    ];
    if (ipo.listed_date) {
      const year = new Date(ipo.listed_date).getFullYear();
      searchUrls.unshift(`https://www.chittorgarh.com/report/ipo-in-india-list-main-board-sme/82/all/?year=${year}`);
      searchUrls.unshift(`https://www.chittorgarh.com/ipo_perf_tracker.asp?exchange=mainline&year=${year}`);
    }

    for (const url of searchUrls) {
      if (detailUrl) break;
      try {
        const response = await axios.get(url, {
          headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' },
          timeout: 10000
        });
        const $ = cheerio.load(response.data);
        $('a').each((_, el) => {
          const text = $(el).text().trim().toLowerCase();
          const href = $(el).attr('href');
          if (href && href.includes('/ipo/') && text.includes(ipoName.toLowerCase().split(' ')[0])) {
            detailUrl = href;
            if (!detailUrl.startsWith('http')) {
               detailUrl = 'https://www.chittorgarh.com' + detailUrl;
            }
          }
        });
      } catch (e: any) {
         // ignore fetch errors and try next
      }
    }
  }

  // Fallback if still no URL: we cannot reliably scrape the detail page
  if (!detailUrl) {
    console.log(`Could not find detail URL for ${ipoName}. Skipping detail scrape.`);
    return ipo;
  }

  console.log(`Detail URL found: ${detailUrl}`);
  
  try {
    const response = await axios.get(detailUrl, {
      headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' },
      timeout: 15000
    });
    const $ = cheerio.load(response.data);
    
    // Robust parser function
    const extractField = (labels: string[], parseFn: (val: string) => any) => {
      let foundValue = null;
      $('*').each((_, el) => {
        if (foundValue !== null) return;
        const elText = $(el).text().trim().toLowerCase();
        
        // Exact match or contains one of the labels (heuristics)
        if (labels.some(l => elText === l || (elText.includes(l) && elText.length < l.length + 10))) {
          // Check sibling or next element in DOM/Table
          let valStr = '';
          const nextTd = $(el).next('td').text().trim();
          const parentNextTd = $(el).parent().children().eq(1).text().trim();
          const nextEl = $(el).next().text().trim();
          
          if (nextTd) valStr = nextTd;
          else if (parentNextTd && parentNextTd !== elText) valStr = parentNextTd;
          else if (nextEl) valStr = nextEl;

          if (valStr) {
            const parsed = parseFn(valStr);
            if (parsed !== null && !Number.isNaN(parsed)) {
              foundValue = parsed;
            }
          }
        }
      });
      return foundValue;
    };

    const extractNumbers = (str: string) => {
      const match = str.replace(/,/g, '').match(/(\d[\d.]*)/);
      return match ? parseFloat(match[1]) : null;
    };

    const issue_price = extractField(['issue price', 'price band'], (str) => {
      // If price band like "₹50 to ₹53", take the upper band
      const matches = str.replace(/,/g, '').match(/([\d.]+)/g);
      if (matches && matches.length > 0) {
        return parseFloat(matches[matches.length - 1]);
      }
      return null;
    });

    const issue_size_cr = extractField(['issue size', 'issue size (cr)', 'issue size (crs)'], extractNumbers);
    const pe_ratio = extractField(['p/e (x)', 'p/e', 'pe ratio', 'post pe', 'pre pe'], extractNumbers);
    const roe = extractField(['roe', 'roe (%)'], extractNumbers);
    const debt_equity = extractField(['debt/equity', 'debt to equity', 'debt-equity'], extractNumbers);
    const qib_sub = extractField(['qib', 'qib subscription'], extractNumbers);
    const hni_sub = extractField(['nii (hni)', 'nii', 'hni', 'hni subscription'], extractNumbers);
    const rii_sub = extractField(['retail', 'retail subscription', 'rii'], extractNumbers);
    const anchor_pct = extractField(['anchor portion', 'anchor allocation'], extractNumbers);
    const gmp_percent = extractField(['gmp', 'grey market premium'], extractNumbers);
    
    // Also try to find subscription total
    let subscription_total = extractField(['total subscription', 'total', 'subscription (x)'], extractNumbers);

    // Save back to DB, keeping existing if we didn't find new
    db.prepare(`
      UPDATE ipos SET 
        issue_price = COALESCE(?, issue_price),
        issue_size_cr = COALESCE(?, issue_size_cr),
        pe_ratio = COALESCE(?, pe_ratio),
        roe = COALESCE(?, roe),
        debt_equity = COALESCE(?, debt_equity),
        qib_sub = COALESCE(?, qib_sub),
        hni_sub = COALESCE(?, hni_sub),
        rii_sub = COALESCE(?, rii_sub),
        anchor_pct = COALESCE(?, anchor_pct),
        gmp_percent = COALESCE(?, gmp_percent),
        subscription_total = COALESCE(?, subscription_total),
        source_url = ?,
        data_source = 'Chittorgarh',
        data_fetched_at = datetime('now'),
        last_updated = datetime('now')
      WHERE name = ?
    `).run(
      issue_price, issue_size_cr, pe_ratio, roe, debt_equity, 
      qib_sub, hni_sub, rii_sub, anchor_pct, gmp_percent, subscription_total,
      detailUrl, ipoName
    );
    
    console.log(`Successfully parsed and updated ${ipoName}`);

  } catch (error: any) {
    console.error(`Failed to fetch/parse detail page for ${ipoName}:`, error.message);
  }

  return db.prepare('SELECT * FROM ipos WHERE name = ?').get(ipoName);
}

export async function fetchLiveSubscriptionAndGMP(ipoName: string) {
  console.log(`Fetching live data for: ${ipoName}`);
  const db = getDb();
  let sub = null;
  
  try {
    const response = await axios.get(CHITTORGARH_MAIN, {
      headers: { 'User-Agent': 'Mozilla/5.0' },
      timeout: 10000
    });
    const $ = cheerio.load(response.data);
    
    $('table tr').each((_, tr) => {
      const text = $(tr).text();
      if (text.toLowerCase().includes(ipoName.toLowerCase().split(' ')[0])) {
        const matchSub = text.match(/([\d.]+)\s*x/);
        if (matchSub) {
          sub = parseFloat(matchSub[1]);
        }
      }
    });
    
    if (sub) {
      db.prepare(`
        UPDATE ipos SET 
          subscription_total = ?, 
          data_fetched_at = datetime('now'),
          last_updated = datetime('now') 
        WHERE name = ?
      `).run(sub, ipoName);
    }
  } catch (error: any) {
    console.error('Failed to fetch live sub:', error.message);
  }
}

export async function verifyAndCorrectData(ipoName: string) {
  console.log(`Verifying data for: ${ipoName}`);
  // In a real scenario, this would compare DB values against fresh Chittorgarh / NSE data
  // For now we'll just run fetchIPODetailFromChittorgarh
  return fetchIPODetailFromChittorgarh(ipoName);
}
