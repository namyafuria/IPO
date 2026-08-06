import axios from 'axios';
import cheerio from 'cheerio';
import { db, IPO } from './db';

const HEADERS = {
  'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
};

// Robust Stopwords to normalize names
const STOPWORDS = ['ltd', 'limited', 'company', 'co', 'corp', 'corporation', 'private', 'pvt', 'enterprises', 'enterprise', 'and', '&', 'the', 'of'];

function normalizeName(name: string): string {
  return name.toLowerCase()
    .replace(/[^a-z0-9\s]/g, '') // remove punctuation
    .split(/\s+/)
    .filter(word => !STOPWORDS.includes(word))
    .join(' ');
}

// Lowered threshold for matching in lists
function nameMatchScore(a: string, b: string): number {
  const normA = normalizeName(a);
  const normB = normalizeName(b);
  if (normA === normB) return 1.0;
  const wordsA = new Set(normA.split(' '));
  const wordsB = new Set(normB.split(' '));
  const intersection = new Set([...wordsA].filter(x => wordsB.has(x)));
  const union = new Set([...wordsA, ...wordsB]);
  return intersection.size / union.size;
}

// Source 1: Performance summary widget (server-rendered, no JS)
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
      // Use 0.45 threshold to catch "Ltd" variations
      const score = nameMatchScore(text, ipoName);
      if (score > bestScore) {
        bestScore = score;
        const href = $(el).attr('href')!;
        bestUrl = href.startsWith('http') ? href : `https://www.chittorgarh.com${href}`;
      }
    });

    return bestScore >= 0.45 ? bestUrl : null;
  } catch (e) {
    return null;
  }
}

// Source 2: Chittorgarh's own search endpoint (replaces the flaky DDG fallback)
async function findDetailUrlViaSiteSearch(ipoName: string): Promise<string | null> {
  try {
    const query = `${ipoName} ipo`;
    const url = `https://www.chittorgarh.com/search.asp?search=${encodeURIComponent(query)}`;
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

    return bestScore >= 0.45 ? bestUrl : null;
  } catch (e: any) {
    console.warn(`Site search failed for "${ipoName}": ${e.message}`);
    return null;
  }
}

// Main discovery function
async function findDetailUrl(ipoName: string, listedDate: string | null): Promise<string | null> {
  // Handle years if listedDate is null
  const year = listedDate ? new Date(listedDate).getFullYear() : new Date().getFullYear();

  // 1. Try performance tracker (current year, then previous year)
  const fromSummary = await findDetailUrlFromPerfSummary(ipoName, year)
    ?? await findDetailUrlFromPerfSummary(ipoName, year - 1);
  if (fromSummary) return fromSummary;

  // 2. Fallback to Chittorgarh's internal search (safe, no DDG block)
  const fromSearch = await findDetailUrlViaSiteSearch(ipoName);
  if (fromSearch) return fromSearch;

  return null;
}

export async function fetchIPODetailFromChittorgarh(ipo: IPO): Promise<IPO> {
  console.log(`Fetching details from Chittorgarh for: ${ipo.name}`);
  
  // Find the detail URL
  const detailUrl = await findDetailUrl(ipo.name, ipo.listingDate || null);

  if (!detailUrl) {
    console.warn(`Could not confidently find a detail URL for ${ipo.name}. Skipping detail scrape.`);
    return ipo; // Return original
  }

  console.log(`Found detail URL: ${detailUrl}`);
  // ... rest of your existing scrape logic follows here ...
  // (You can keep the rest of this function exactly as you had it, 
  // it just needs the `findDetailUrl` helper above to work first!)
  return ipo; 
}
