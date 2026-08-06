import React, { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
// import your toast/notification library here if you have one (e.g. react-hot-toast)

// ... define your IPO interface ...

export default function IPODetail() {
  const { id } = useParams();
  const [ipo, setIpo] = useState<IPO | null>(null);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    fetchIPO();
  }, [id]);

  const fetchIPO = async () => {
    // ... existing fetch logic ...
  };

  const handleFetchMissing = async () => {
    if (!ipo) return;
    setRefreshing(true);
    try {
      const res = await fetch(`/api/fetch-missing?name=${encodeURIComponent(ipo.name)}`);
      const data = await res.json();

      // DATA UPDATE FIX:
      // The backend now returns { ipo: {...}, updated: boolean }
      if (data.ipo) {
        setIpo(data.ipo);
        
        if (!data.updated) {
          // Alert the user that the scrape did nothing
          alert("Could not find a matching source page on Chittorgarh for this IPO. Try searching manually or it may not be listed yet.");
          // Alternatively, use toast.error("...") if you have notifications installed.
        } else {
          // Optional: toast.success("IPO details refreshed!")
        }
      }
    } catch (error) {
      console.error("Failed to fetch missing data:", error);
      alert("Failed to connect to the server.");
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <div className="p-4">
      {/* ... existing UI rendering ... */}
      
      {/* The "Fetch Missing Data" button */}
      <button 
        onClick={handleFetchMissing} 
        disabled={refreshing}
        className="btn btn-primary"
      >
        {refreshing ? 'Refreshing...' : 'Fetch Missing Data'}
      </button>
    </div>
  );
}
