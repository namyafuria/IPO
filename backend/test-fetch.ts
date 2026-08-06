import { fetchIPODetailFromChittorgarh } from './fetcher';

async function run() {
  const ipoName = process.argv[2];
  if (!ipoName) {
    console.error('Usage: npm run test-fetch -- "IPO Name"');
    process.exit(1);
  }

  console.log(`Running test fetch for IPO: ${ipoName}`);
  const result = await fetchIPODetailFromChittorgarh(ipoName);
  
  if (result) {
    console.log('\n--- Final Parsed Database Row ---');
    console.log(JSON.stringify(result, null, 2));
  } else {
    console.log(`\nFailed to fetch or find IPO: ${ipoName}`);
  }
}

run();
