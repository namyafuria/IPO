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
  const clean =
