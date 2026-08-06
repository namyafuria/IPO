import axios from 'axios';
import * as cheerio from 'cheerio';
import type { CheerioAPI } from 'cheerio';
import { getDb } from './db';

const CHITTORGARH_MAIN = 'https://www.chittorgarh.com/report/ipo-in-india-list-main-board-sme/82/';
const HEADERS = {
  'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'
};

const STOPWORDS = new Set(['ltd', 'limited', 'ipo', 'invit', 'trust', 'the', 'and', 'of']);

function normalizeName(name: string): string[] {
  return name
    .toLowerCase()
    .replace(/[()]/g, ' ')
    .replace(/[^a-z0-9]+/g, ' ')
    .split(' ')
    .filter(w => w.length > 0 && !STOPWORDS.has(w));
}

function nameMatchScore(candidateText: string, targetName: string): number {
  const targetWords = normalizeName(targetName);
  if (targetWords.length === 0) return 0;
  const candidateWords = new Set(normalizeName(candidateText));
  let hits = 0;
  for (const w of targetWords) if (candidateWords.has(w)) hits++;
  return hits / targetWords.length;
}

function extractFirstNumber(str: string): number | null {
  const match = str.replace(/,/g, '').match(/(-?[\d.]+)/);
  return match ? parseFloat(match[1]) : null;
}

function extractCroreAmount(str: string): number | null {
  const clean = str.replace(/,/g, '');
  const crMatch = clean.match(/([\d.]+)\s*(?:cr\.?|crore)\b/i);
  if (crMatch) return parseFloat(crMatch[1]);
  return extractFirstNumber(clean);
}

function extractUpperPrice(str: string): number | null {
  const clean = str.replace(/,/g, '');
  const matches = clean.match(/([\d.]+)/g);
  if (!matches || matches.length === 0) return null;
  return parseFloat(matches[matches.length - 1]);
}

function isOwnDetailTable($: CheerioAPI, table: any): boolean {
  const $table = $(table);
  const ipoLinks = $table.find('a[href*="/ipo/"]').length;
  if (ipoLinks > 1) return false;
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
// Detail-page URL discovery.
// Chittorgarh's main list pages load their table via JavaScript, so a plain
// server-side request sees an empty page. These two sources are actually
// server-rendered (visible without JavaScript), so we use those instead.
// ---------------------------------------------------------------------------

async function findDetailUrlFromPerfSummary(ipoName: string, year: number): Promise<string | null> {
  try {
    const url = `https://www.chittorgarh.com/ipo/ipo_perf_tracker.asp?exchange=mainline&year=${year}`;
    const response = await axios.get(url, { headers: HEADERS, timeout: 10000 });
    const $ = cheerio.load(response.data);

    let bestUrl: string | null = null;
    let bestScore = 0;

    $('a[href*="/ipo/"]').each((_, el) => {
      const text = $(el).text().trim();
      if (!text) return;
      const score = nameMatchScore(text, ipoName);
      if (score > bestScore) {
        bestScore = score;
        const href = $(el).attr('href')!;
        bestUrl = href.startsWith('http') ? href : `https://www.chittorgarh.com${href}`;
      }
    });

    return bestScore >= 0.6 ? bestUrl : null;
  } catch {
    return null;
  }
}

async function findDetailUrlViaSearch(ipoName: string): Promise<string | null> {
  try {
    const query = `${ipoName} ipo site:chittorgarh.com`;
    const url = `https://html.duckduckgo.com/html/?q=${encodeURIComponent(query)}`;
    const response = await axios.get(url, { headers: HEADERS, timeout: 10000 });
    const $ = cheerio.load(response.data);

    let bestUrl: string | null = null;
    let bestScore = 0;

    $('a.result__a').each((_, el) => {
      let href = $(el).attr('href') || '';
      const uddg = href.match(/[?&]uddg=([^&]+)/);
      if (uddg) href = decodeURIComponent(uddg[1]);
      if (!href.includes('chittorgarh.com/ipo/')) return;

      const text = $(el).text().trim();
      const score = nameMatchScore(text, ipoName);
      if (score > bestScore) {
        bestScore = score;
        bestUrl = href;
      }
    });

    return bestScore >= 0.6 ? bestUrl : null;
  } catch (e: any) {
    console.warn(`DDG search failed for "${ipoName}": ${e.message}`);
    return null;
  }
}

async function findDetailUrl(ipoName: string, listedDate: string | null): Promise<string | null> {
  const year = listedDate ? new Date(listedDate).getFullYear() : new Date().getFullYear();

  const fromSummary =
    (await findDetailUrlFromPerfSummary(ipoName, year)) ??
    (await findDetailUrlFromPerfSummary(ipoName, year - 1));
  if (fromSummary) return fromSummary;

  return findDetailUrlViaSearch(ipoName);
}

export async function fetchIPODetailFromChittorgarh(ipoName: string) {
  console.log(`Fetching details from Chittorgarh for: ${ipoName}`);
  const db = getDb();

  let ipo = db.prepare('SELECT * FROM ipos WHERE name = ?').get(ipoName) as any;
  if (!ipo) {
    console.error(`IPO ${ipoName} not found in DB.`);
    return { ipo: null, updated: false };
  }

  let detailUrl: string | null = ipo.source_url || null;

  if (!detailUrl) {
    console.log(`No source_url for ${ipoName}, searching main lists...`);
    detailUrl = await findDetailUrl(ipoName, ipo.listed_date);
  }

  if (!detailUrl) {
    console.log(`Could not confidently find a detail URL for ${ipoName}. Skipping detail scrape.`);
    return { ipo, updated: false };
  }

  console.log(`Detail URL found: ${detailUrl}`);

  try {
    const response = await axios.get(detailUrl, { headers: HEADERS, timeout: 15000 });
    const $ = cheerio.load(response.data);

    const pageTitle = $('h1').first().text() || $('title').text();
    if (nameMatchScore(pageTitle, ipoName) < 0.4) {
      console.warn(`Fetched page does not look like it's about "${ipoName}" (title: "${pageTitle.trim()}"). Skipping to avoid corrupting data.`);
      return { ipo, updated: false };
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
    return { ipo: db.prepare('SELECT * FROM ipos WHERE name = ?').get(ipoName), updated: true };

  } catch (error: any) {
    console.error(`Failed to fetch/parse detail page for ${ipoName}:`, error.message);
    return { ipo, updated: false };
  }
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
