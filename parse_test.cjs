const cheerio = require('cheerio');
const html = `
<table class="table table-bordered table-striped table-hover mt-3 w-100">
  <tbody>
    <tr><td>Issue Price</td><td>₹225 per share</td></tr>
    <tr><td>Issue Size</td><td>₹3750.00 Cr</td></tr>
    <tr><td>PE Ratio</td><td>35.5</td></tr>
  </tbody>
</table>
`;
const $ = cheerio.load(html);
let issuePrice = null;
$('*').each((i, el) => {
  const text = $(el).text().trim().toLowerCase();
  if (text === 'issue price' || text === 'price band') {
    const nextText = $(el).next().text().trim() || $(el).parent().next().text().trim() || $(el).parent().children().eq(1).text().trim();
    console.log("Found:", nextText);
  }
});
