const axios = require('axios');
const cheerio = require('cheerio');
async function run() {
  const res = await axios.get('https://html.duckduckgo.com/html/?q=site:chittorgarh.com+ipo+Cube+Highways', {
    headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)' }
  });
  console.log(res.data.substring(0, 500));
}
run();
