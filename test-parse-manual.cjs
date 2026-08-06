const str = "[.] Aggregating up to ₹3,802.50 Cr";
const match = str.replace(/,/g, '').match(/(\d[\d.]*)/);
console.log(match ? parseFloat(match[1]) : null);
