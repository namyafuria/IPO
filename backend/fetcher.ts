import axios from 'axios';
import * as cheerio from 'cheerio';
import type { CheerioAPI } from 'cheerio';
import { getDb } from './db';

const CHITTORGARH_MAIN = 'https://www.chittorgarh.com/report/ipo-in-india-list-main-board-sme/82/';
const HEADERS = {
  'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'
};

// ---------------------------------------------------------------------------
// Name matching helpers
//
// Chittorgarh pages are full of cross-links: "Recently Listed IPOs in
// <sector>" comparison widgets, "Similar IPOs" boxes, breadcrumb links, etc.
// Any matching that keys off "does this link's text contain the first word
// of the company name" is going to false-positive constantly. We instead
// normalize both names and score word overlap, and always keep the BEST
// scoring candidate seen (not the last one, and not the first "any" match).
// ---------------------------------------------------------------------------

const STOPWORDS = new Set(['ltd', 'limited', 'ipo', 'invit', 'trust', 'the', 'and', 'of']);

function normalizeName(name: string): string[] {
  return name
    .toLowerCase()
    .replace(/[()]/g, ' ')
    .replace(/[^a-z0-9]+/g, ' ')
    .split(' ')
    .filter(w => w.length > 0 && !STOPWORDS.has(w));
}

// Fraction of the target's significant words that appear in the candidate string.
function nameMatchScore(candidateText: string, targetName: string): number {
  const targetWords = normalizeName(targetName);
  if (targetWords.length === 0) return 0;
  const candidateWords = new Set(normalizeName(candidateText));
  let hits = 0;
  for (const w of targetWords) if (candidateWords.has(w)) hits++;
  return hits / targetWords.length;
}

// ---------------------------------------------------------------------------
// Number extraction helpers
// ---------------------------------------------------------------------------

// Grabs the first plain number in a string. Only safe to use on cells that
// are known to contain a single meaningful number (e.g. "35.5", "18.06%").
function extractFirstNumber(str: string): number | null {
  const match = str.replace(/,/g, '').match(/(-?[\d.]+)/);
  return match ? parseFloat(match[1]) : null;
}

// Chittorgarh often formats size cells as compound strings like:
//   "32,89,47,365 shares    (agg. up to ₹5,000 Cr)"
//   "[.] Aggregating up to ₹3,802.50 Cr"
// A naive "first number in the string" grab picks up the share count, not
// the crore figure. Prefer a number that's explicitly followed by "Cr"/
// "Crore"; only fall back to "first number" if no such marker exists.
function extractCroreAmount(str: string): number | null {
  const clean = str.replace(/,/g, '');
  const crMatch = clean.match(/([\d.]+)\s*(?:cr\.?|crore)\b/i);
  if (crMatch) return parseFloat(crMatch[1]);
  return extractFirstNumber(clean);
}

// For "Issue Price" / "Price Band" cells like "₹151.00 to ₹152.00" or
// "₹50 to ₹53 per share", take the upper bound of the band.
function extractUpperPrice(str: string): number | null {
  const clean = str.replace(/,/g, '');
  const matches = clean.match(/([\d.]+)/g);
  if (!matches || matches.length === 0) return null;
  return parseFloat(matches[matches.length - 1]);
}

// ---------------------------------------------------------------------------
// Table scoping
//
// A genuine "company detail" table on Chittorgarh is a simple label/value
// table: each row has exactly two <td> cells, and it doesn't link out to
// more than one other IPO's detail page. The "Recently Listed IPOs in
// <sector>" widget and similar comparison tables are multi-column, have a
// header row, and link to several different companies' /ipo/ pages -- we
// must exclude those or we'll happily read another company's numbers.
// ---------------------------------------------------------------------------

function isOwnDetailTable($: CheerioAPI, table: any): boolean {
  const $table = $(table);

  // Comparison / "recently listed" widgets link to multiple other IPOs.
  const ipoLinks = $table.find('a[href*="/ipo/"]').length;
  if (ipoLinks > 1) return false;

  // A genuine detail table has no header row and is strictly 2 columns wide
  // wherever it has data cells at all.
  if ($table.find('th').length > 0) return false;

  let hasRows = false;
  let allTwoCol = true;
  $table.find('tr').each((_, tr) => {
    const tds = $(tr).find('td');
    if (tds.length === 0) return;
    hasRows = true;
    if (tds.length !== 2) allTwoCol = false;
  });

  return hasRows && allTwoCol;
}

// Extract a field by scanning only genuine label/value tables on the page.
// Returns the parsed value from the FIRST matching row found (matching the
// page's natural top-to-bottom priority: primary details before secondary
// financial/ratio tables).
function extractField(
  $: CheerioAPI,
  labels: string[],
  parseFn: (val: string) => number | null
): number | null {
  const candidateTables = $('table').filter((_, t) => isOwnDetailTable($, t));

  let found: number | null = null;

  candidateTables.each((_, table) => {
    if (found !== null) return;
    $(table).find('tr').each((__, tr) => {
      if (found !== null) return;
      const tds = $(tr).find('td');
      if (tds.length !== 2) return;

      const labelText = $(tds[0]).text().trim().toLowerCase();
      const valueText = $(tds[1]).text().trim();
      if (!valueText) return;

      const isMatch = labels.some(l =>
        labelText === l || (labelText.includes(l) && labelText.length < l.length + 15)
      );
      if (!isMatch) return;

      const parsed = parseFn(valueText);
      if (parsed !== null && !Number.isNaN(parsed)) {
        found = parsed;
      }
    });
  });

  return found;
}

// Same lookup as extractField, but returns the raw matched cell text instead
// of a parsed number. Used where a value needs custom splitting (e.g. a
// "X to Y" price band needing both bounds, not just the upper one).
function extractRawFieldText($: CheerioAPI, labels: string[]): string | null {
  const candidateTables = $('table').filter((_, t) => isOwnDetailTable($, t));
  let found: string | null = null;

  candidateTables.each((_, table) => {
    if (found !== null) return;
    $(table).find('tr').each((__, tr) => {
      if (found !== null) return;
      const tds = $(tr).find('td');
      if (tds.length !== 2) return;

      const labelText = $(tds[0]).text().trim().toLowerCase();
      const valueText = $(tds[1]).text().trim();
      if (!valueText) return;

      const isMatch = labels.some(l =>
        labelText === l || (labelText.includes(l) && labelText.length < l.length + 15)
      );
      if (isMatch) found = valueText;
    });
  });

  return found;
}

// ---------------------------------------------------------------------------
// List discovery (unchanged in spirit, kept as-is for the yearly index)
// ---------------------------------------------------------------------------

export async function fetchIPOListFromChittorgarh(year: number) {
  console.log(`Fetching IPO list for year ${year} from Chittorgarh...`);
  const db = getDb();
  let added = 0;

  try {
    const url = `https://www.chittorgarh.com/ipo_perf_tracker.asp?exchange=mainline&year=${year}`;
    const response = await axios.get(url, { headers: HEADERS, timeout: 15000 });
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

// ---------------------------------------------------------------------------
// Detail-page URL discovery — rewritten to keep the BEST scoring match,
// not the LAST one found, and to score on the whole name, not just the
// first word.
// ---------------------------------------------------------------------------

async function findDetailUrl(ipoName: string, listedDate: string | null): Promise<string | null> {
  const searchUrls: string[] = [
    'https://www.chittorgarh.com/report/ipo-in-india-list-main-board-sme/82/all/',
    'https://www.chittorgarh.com/report/ipo-in-india-list-main-board-sme/82/',
  ];
  if (listedDate) {
    const year = new Date(listedDate).getFullYear();
    searchUrls.unshift(`https://www.chittorgarh.com/report/ipo-in-india-list-main-board-sme/82/all/?year=${year}`);
    searchUrls.unshift(`https://www.chittorgarh.com/ipo_perf_tracker.asp?exchange=mainline&year=${year}`);
  }

  let bestUrl: string | null = null;
  let bestScore = 0;

  for (const url of searchUrls) {
    // A near-perfect match on an earlier URL is good enough — stop early.
    if (bestScore >= 0.999) break;

    try {
      const response = await axios.get(url, { headers: HEADERS, timeout: 10000 });
      const $ = cheerio.load(response.data);

      $('a').each((_, el) => {
        const href = $(el).attr('href');
        if (!href || !href.includes('/ipo/')) return;

        const text = $(el).text().trim();
        if (!text) return;

        const score = nameMatchScore(text, ipoName);
        if (score > bestScore) {
          bestScore = score;
          bestUrl = href.startsWith('http') ? href : `https://www.chittorgarh.com${href}`;
        }
      });
    } catch (e: any) {
      // ignore fetch errors and try next source
    }
  }

  // Require a reasonably strong overlap before trusting the match — a weak
  // score means we likely didn't actually find this company on the page.
  return bestScore >= 0.6 ? bestUrl : null;
}

export async function fetchIPODetailFromChittorgarh(ipoName: string) {
  console.log(`Fetching details from Chittorgarh for: ${ipoName}`);
  const db = getDb();

  let ipo = db.prepare('SELECT * FROM ipos WHERE name = ?').get(ipoName) as any;
  if (!ipo) {
    console.error(`IPO ${ipoName} not found in DB.`);
    return null;
  }

  let detailUrl: string | null = ipo.source_url || null;

  if (!detailUrl) {
    console.log(`No source_url for ${ipoName}, searching main lists...`);
    detailUrl = await findDetailUrl(ipoName, ipo.listed_date);
  }

  if (!detailUrl) {
    console.log(`Could not confidently find a detail URL for ${ipoName}. Skipping detail scrape.`);
    return ipo;
  }

  console.log(`Detail URL found: ${detailUrl}`);

  try {
    const response = await axios.get(detailUrl, { headers: HEADERS, timeout: 15000 });
    const $ = cheerio.load(response.data);

    // Sanity check: make sure the page we landed on is actually about this
    // company, and not a mismatch that slipped past the URL scoring above.
    const pageTitle = $('h1').first().text() || $('title').text();
    if (nameMatchScore(pageTitle, ipoName) < 0.4) {
      console.warn(`Fetched page does not look like it's about "${ipoName}" (title: "${pageTitle.trim()}"). Skipping to avoid corrupting data.`);
      return ipo;
    }

    const issue_price = extractField($, ['issue price', 'price band', 'final issue price'], extractUpperPrice);
    const issue_size_cr = extractField($, ['issue size', 'total issue size'], extractCroreAmount);
    const pe_ratio = extractField($, ['p/e (x)', 'p/e', 'pe ratio', 'post ipo p/e', 'pre ipo p/e'], extractFirstNumber);
    const roe = extractField($, ['roe', 'roe (%)', 'return on equity'], extractFirstNumber);
    const debt_equity = extractField($, ['debt/equity', 'debt to equity', 'debt-equity'], extractFirstNumber);
    const qib_sub = extractField($, ['qib', 'qib subscription', 'qib (ex anchor)'], extractFirstNumber);
    const hni_sub = extractField($, ['nii (hni)', 'nii', 'hni', 'hni subscription'], extractFirstNumber);
    const rii_sub = extractField($, ['retail', 'retail subscription', 'rii'], extractFirstNumber);
    const anchor_pct = extractField($, ['anchor portion', 'anchor allocation'], extractFirstNumber);
    const gmp_percent = extractField($, ['gmp', 'grey market premium'], extractFirstNumber);
    const subscription_total = extractField($, ['total subscription', 'subscription (x)'], extractFirstNumber);

    let price_band_low: number | null = null;
    let price_band_high: number | null = null;
    const bandText = extractRawFieldText($, ['price band']);
    if (bandText) {
      const nums = bandText.replace(/,/g, '').match(/([\d.]+)/g);
      if (nums && nums.length >= 2) {
        price_band_low = parseFloat(nums[0]);
        price_band_high = parseFloat(nums[nums.length - 1]);
      }
    }

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
        price_band_low = COALESCE(?, price_band_low),
        price_band_high = COALESCE(?, price_band_high),
        source_url = ?,
        data_source = 'Chittorgarh',
        data_fetched_at = datetime('now'),
        last_updated = datetime('now')
      WHERE name = ?
    `).run(
      issue_price, issue_size_cr, pe_ratio, roe, debt_equity,
      qib_sub, hni_sub, rii_sub, anchor_pct, gmp_percent, subscription_total,
      price_band_low, price_band_high,
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

  try {
    const response = await axios.get(CHITTORGARH_MAIN, { headers: HEADERS, timeout: 10000 });
    const $ = cheerio.load(response.data);

    let bestSub: number | null = null;
    let bestScore = 0;

    $('table tr').each((_, tr) => {
      const $tr = $(tr);
      const rowText = $tr.text();
      // The company name is normally the text of the first link in the row.
      const nameCellText = $tr.find('a').first().text().trim() || $tr.find('td').first().text().trim();
      if (!nameCellText) return;

      const score = nameMatchScore(nameCellText, ipoName);
      if (score < 0.6 || score < bestScore) return;

      const matchSub = rowText.match(/([\d.]+)\s*x/i);
      if (matchSub) {
        bestScore = score;
        bestSub = parseFloat(matchSub[1]);
      }
    });

    if (bestSub !== null) {
      db.prepare(`
        UPDATE ipos SET 
          subscription_total = ?, 
          data_fetched_at = datetime('now'),
          last_updated = datetime('now') 
        WHERE name = ?
      `).run(bestSub, ipoName);
    }
  } catch (error: any) {
    console.error('Failed to fetch live sub:', error.message);
  }
}

export async function verifyAndCorrectData(ipoName: string) {
  console.log(`Verifying data for: ${ipoName}`);
  return fetchIPODetailFromChittorgarh(ipoName);
}
