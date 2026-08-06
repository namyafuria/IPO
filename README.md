# IPO Analyser

IPO Analyser is a full-stack web application designed to help users understand how an upcoming Indian mainboard IPO might perform on its listing day by comparing its subscription multiple against historical IPOs. 

It tracks listed IPOs and provides a dashboard, searchable list, detailed views, and a weighted prediction engine.

## Tech Stack

- **Backend**: Node.js, Express, SQLite (sqlite3 with sqlite wrapper), node-cache, node-cron.
- **Frontend**: React, React Router, Tailwind CSS, Axios, Lucide React.
- **Build**: Vite (frontend), esbuild (backend).

## Project Structure

This project follows a streamlined full-stack architecture optimized for ease of deployment:
- \`/src\`: Contains the frontend React application.
- \`/backend\`: Contains the Node.js Express server, SQLite database setup, and background refresh jobs.
- The root directory contains configuration files like \`package.json\` and \`vite.config.ts\`.

## Local Development

1. **Install Dependencies**:
   \`\`\`bash
   npm install
   \`\`\`

2. **Run the Development Server**:
   \`\`\`bash
   npm run dev
   \`\`\`
   This will start the Express backend on port 3000 (by default). The backend automatically sets up Vite middleware, serving both the API and the React frontend concurrently. The database is automatically seeded on the first run.

3. **Scrape Extended Historical Data**:
   \`\`\`bash
   npm run scrape-history
   \`\`\`
   This runs a script (\`backend/scrape_history.ts\`) to fetch historical IPO data (2016-2021) from external trackers, enriching the Predictor's dataset.

4. **Dynamic Data Fetching**:
   The application now includes a robust dynamic fetching pipeline from Chittorgarh and Groww.
   - Run \`npx tsx backend/migrate.ts\` to ensure the schema has the new fetching metadata columns.

## Deployment Instructions (Render, Railway, etc.)

This application is designed to be easily deployed as a single web service. 

1. **Build the Application**:
   \`\`\`bash
   npm run build
   \`\`\`
   This command builds both the Vite frontend (into \`dist/\`) and the Node.js backend (into \`dist/server.cjs\`).

2. **Start the Production Server**:
   \`\`\`bash
   npm run start
   \`\`\`
   The production server will serve the static React files from the \`dist\` directory and handle all \`/api\` requests.

### Configuration for Cloud Providers
- **Build Command**: \`npm run build\`
- **Start Command**: \`npm run start\`
- **Environment Variables**: You can optionally configure the \`PORT\` variable. 
- **Persistent Storage**: If you want the SQLite database to persist across deployments, ensure you mount a persistent volume to the \`/backend/data\` path.

## Key Features
- **Dashboard**: High-level market overview including total tracked IPOs, median gain, and positive listing rate.
- **Weighted Prediction Engine**: Predict an IPO's listing gain using a Gaussian kernel weighting model that matches similar historical subscription numbers.
- **Automated Refreshes**: A built-in cron job securely refreshes data daily and scans for new IPOs.
- **Caching**: API endpoints utilize \`node-cache\` to minimize database load.
