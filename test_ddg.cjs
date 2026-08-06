const axios = require('axios');
async function run() {
  try {
    const res = await axios.get('https://html.duckduckgo.com/html/?q=site:chittorgarh.com+ipo+Cube+Highways', {
      headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)' }
    });
    console.log(res.data.match(/https:\/\/www\.chittorgarh\.com\/ipo\/[^/]+\/\d+\//g));
  } catch (e) { console.error(e.message); }
}
run();
