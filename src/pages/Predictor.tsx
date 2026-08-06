import { useState, useEffect } from 'react';
import { predictWeighted } from '../lib/api';
import { AlertTriangle, TrendingUp, Calculator, Target, Search, Info } from 'lucide-react';
import { Link } from 'react-router-dom';
import { useDebounce } from 'use-debounce';
import axios from 'axios';

export default function Predictor() {
  const [companyName, setCompanyName] = useState<string>('');
  const [debouncedName] = useDebounce(companyName, 500);
  const [ipoDetails, setIpoDetails] = useState<any>(null);
  const [fetchingDetails, setFetchingDetails] = useState(false);
  const [notFound, setNotFound] = useState(false);

  const [subscription, setSubscription] = useState<string>('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!debouncedName.trim()) {
      setIpoDetails(null);
      setNotFound(false);
      return;
    }
    
    const fetchDetails = async () => {
      setFetchingDetails(true);
      setNotFound(false);
      try {
        const response = await axios.get(`/api/ipo-details?name=${encodeURIComponent(debouncedName)}`);
        setIpoDetails(response.data);
        if (response.data.subscription_total) {
          setSubscription(response.data.subscription_total.toString());
        }
      } catch (err: any) {
        setIpoDetails(null);
        if (err.response?.status === 404) {
          setNotFound(true);
        }
      } finally {
        setFetchingDetails(false);
      }
    };
    
    fetchDetails();
  }, [debouncedName]);

  const handleFetchMissing = async () => {
    if (!debouncedName.trim()) return;
    setFetchingDetails(true);
    setNotFound(false);
    try {
      const response = await axios.get(`/api/fetch-missing?name=${encodeURIComponent(debouncedName)}`);
      setIpoDetails(response.data);
      if (response.data.subscription_total) {
        setSubscription(response.data.subscription_total.toString());
      }
    } catch (err) {
      setError('Failed to fetch IPO from external sources.');
    } finally {
      setFetchingDetails(false);
    }
  };

  const handleSubmit = async (e: any) => {
    e.preventDefault();
    setError(null);
    setResult(null);
    
    const subVal = parseFloat(subscription);
    if (isNaN(subVal) || subVal <= 0) {
      setError('Please enter a valid positive number.');
      return;
    }

    setLoading(true);
    try {
      const data = await predictWeighted(subVal);
      setResult(data);
    } catch (err: any) {
      setError(err.response?.data?.error || 'Failed to generate prediction');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-10 animate-in fade-in duration-500">
      <div className="text-center mb-10">
        <h1 className="text-3xl font-serif italic text-zinc-200 mb-4">Listing Gain Predictor</h1>
        <p className="text-zinc-400 max-w-2xl mx-auto text-sm">
          Enter an IPO's expected subscription multiple to see a weighted prediction of its listing day gain, based on historical similarities.
        </p>
      </div>

      <div className="bg-[#18181b] p-6 sm:p-8 rounded-2xl border border-white/5 shadow-2xl mb-8 max-w-2xl mx-auto relative overflow-hidden">
        <div className="absolute -top-10 -right-10 w-32 h-32 bg-emerald-500/10 rounded-full blur-3xl"></div>
        <form onSubmit={handleSubmit} className="relative z-10 flex flex-col gap-6">
          
          <div className="flex flex-col gap-2">
            <label className="text-[10px] uppercase text-zinc-500 tracking-widest font-bold flex justify-between">
              <span>Company Name (Optional)</span>
              {fetchingDetails && <span className="text-emerald-500 animate-pulse">Searching...</span>}
            </label>
            <div className="relative">
              <div className="absolute inset-y-0 left-0 pl-4 flex items-center pointer-events-none">
                <Search className="h-4 w-4 text-zinc-500" />
              </div>
              <input
                type="text"
                className="w-full bg-black/40 border border-white/10 rounded-lg p-3 pl-10 font-sans text-sm focus:outline-none focus:border-emerald-500 text-zinc-200 transition-colors"
                placeholder="e.g. Ardee Industries"
                value={companyName}
                onChange={(e) => setCompanyName(e.target.value)}
                autoComplete="off"
              />
            </div>
            
            {notFound && !fetchingDetails && (
              <div className="bg-white/5 border border-white/10 rounded-lg p-3 mt-1 flex flex-wrap gap-x-6 gap-y-2 text-xs items-center justify-between">
                <span className="text-zinc-400">Not found in database.</span>
                <button
                  type="button"
                  onClick={handleFetchMissing}
                  className="bg-zinc-800 hover:bg-zinc-700 text-zinc-300 font-bold py-1 px-3 rounded text-[10px] uppercase tracking-widest transition-colors"
                >
                  Fetch this IPO
                </button>
              </div>
            )}

            {ipoDetails && (
              <div className="bg-white/5 border border-white/10 rounded-lg p-3 mt-1 flex flex-wrap gap-x-6 gap-y-2 text-xs">
                {ipoDetails.sector && (
                  <div className="flex flex-col">
                    <span className="text-zinc-500 uppercase tracking-widest text-[9px]">Sector</span>
                    <span className="text-zinc-300 font-medium">{ipoDetails.sector}</span>
                  </div>
                )}
                {ipoDetails.issue_price && (
                  <div className="flex flex-col">
                    <span className="text-zinc-500 uppercase tracking-widest text-[9px]">Issue Price</span>
                    <span className="text-zinc-300 font-mono">₹{ipoDetails.issue_price}</span>
                  </div>
                )}
                {ipoDetails.issue_size_cr && (
                  <div className="flex flex-col">
                    <span className="text-zinc-500 uppercase tracking-widest text-[9px]">Issue Size</span>
                    <span className="text-zinc-300 font-mono">₹{ipoDetails.issue_size_cr} Cr</span>
                  </div>
                )}
                <div className="flex flex-col">
                  <span className="text-zinc-500 uppercase tracking-widest text-[9px]">Status</span>
                  <span className={ipoDetails.listing_gain_pct === null ? "text-amber-400 font-medium" : "text-emerald-400 font-medium"}>
                    {ipoDetails.listing_gain_pct === null ? "Upcoming/Active" : "Listed"}
                  </span>
                </div>
              </div>
            )}
          </div>

          <div className="flex flex-col gap-2">
            <label className="text-[10px] uppercase text-zinc-500 tracking-widest font-bold">Est. Subscription Multiple</label>
            <div className="flex flex-col sm:flex-row gap-4">
              <div className="flex-1 relative">
                <input
                  type="number"
                  step="0.01"
                  min="0.01"
                  className="w-full bg-black/40 border border-white/10 rounded-lg p-3 font-mono text-xl focus:outline-none focus:border-emerald-500 text-zinc-200 pl-4 pr-12 transition-colors"
                  placeholder="e.g. 45.50"
                  value={subscription}
                  onChange={(e) => setSubscription(e.target.value)}
                  required
                />
                <div className="absolute inset-y-0 right-0 pr-4 flex items-center pointer-events-none">
                  <span className="text-zinc-500 text-sm font-mono">x</span>
                </div>
              </div>
              <button
                type="submit"
                disabled={loading || !subscription}
                className="bg-emerald-500 text-black font-bold px-8 py-3 sm:py-0 rounded-lg hover:bg-emerald-400 transition-colors uppercase text-xs tracking-widest disabled:opacity-50 flex items-center justify-center min-w-[120px]"
              >
                {loading ? (
                  <div className="animate-spin rounded-full h-5 w-5 border-b-2 border-black"></div>
                ) : (
                  'Run'
                )}
              </button>
            </div>
          </div>
        </form>
        {error && <p className="mt-4 text-sm text-rose-500 relative z-10">{error}</p>}
      </div>

      {result && (
        <div className="animate-in slide-in-from-bottom-4 duration-500">
          <div className="bg-[#18181b] p-6 rounded-2xl border border-white/5 shadow-2xl mb-8 overflow-hidden">
            <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between mb-6">
              <h2 className="text-xl font-serif italic text-zinc-200">Prediction Results</h2>
              <span className="text-xs text-zinc-500 uppercase tracking-widest mt-2 sm:mt-0 font-bold">Based on {result.count} similar IPOs</span>
            </div>
            
            <div className="p-6 bg-black/30 rounded-xl border border-white/5 space-y-6">
              <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
                <span className="text-sm text-zinc-500 uppercase tracking-widest font-bold">Weighted Avg Gain</span>
                <span className={`text-4xl font-mono font-bold ${result.weightedGain > 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                  {result.weightedGain > 0 ? '+' : ''}{result.weightedGain.toFixed(2)}%
                </span>
              </div>
              
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 pt-4 border-t border-white/5">
                <div className="bg-white/5 p-4 rounded-lg">
                  <span className="block text-[10px] text-zinc-500 uppercase tracking-widest mb-1 font-bold">Min</span>
                  <span className="text-lg font-mono text-zinc-300">{result.min > 0 ? '+' : ''}{result.min.toFixed(2)}%</span>
                </div>
                <div className="bg-white/5 p-4 rounded-lg">
                  <span className="block text-[10px] text-zinc-500 uppercase tracking-widest mb-1 font-bold">Max</span>
                  <span className="text-lg font-mono text-zinc-300">{result.max > 0 ? '+' : ''}{result.max.toFixed(2)}%</span>
                </div>
                <div className="bg-white/5 p-4 rounded-lg">
                  <span className="block text-[10px] text-zinc-500 uppercase tracking-widest mb-1 font-bold">Median</span>
                  <span className="text-lg font-mono text-zinc-300">{result.median > 0 ? '+' : ''}{result.median.toFixed(2)}%</span>
                </div>
                <div className="bg-white/5 p-4 rounded-lg">
                  <span className="block text-[10px] text-zinc-500 uppercase tracking-widest mb-1 font-bold">Pos. Rate</span>
                  <span className="text-lg font-mono text-zinc-300">{result.positiveRate.toFixed(1)}%</span>
                </div>
              </div>
            </div>
          </div>

          <div className="bg-[#121214] rounded-2xl border border-white/5 overflow-hidden mb-8">
            <div className="px-6 py-5 border-b border-white/5 bg-white/[0.02]">
              <h3 className="text-lg font-serif italic text-zinc-200">Most Similar Historical IPOs</h3>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-white/[0.02] text-zinc-500 text-[10px] uppercase tracking-tighter border-b border-white/5">
                  <tr className="text-left">
                    <th scope="col" className="px-6 py-4 font-semibold">Company</th>
                    <th scope="col" className="px-6 py-4 font-semibold text-right">Subscription</th>
                    <th scope="col" className="px-6 py-4 font-semibold text-right">Gain</th>
                    <th scope="col" className="px-6 py-4 font-semibold text-right">Similarity</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-white/5">
                  {result.samples.map((sample: any, idx: number) => (
                    <tr key={idx} className="hover:bg-white/[0.02] transition-colors">
                      <td className="px-6 py-4 font-medium">
                        <Link to={`/ipo/${encodeURIComponent(sample.name)}`} className="text-zinc-200 hover:text-emerald-400 transition-colors">
                          {sample.name}
                        </Link>
                      </td>
                      <td className="px-6 py-4 text-zinc-300 text-right font-mono">
                        {sample.sub.toFixed(2)}x
                      </td>
                      <td className="px-6 py-4 text-right font-mono font-bold">
                        <span className={sample.gain > 0 ? 'text-emerald-400' : 'text-rose-400'}>
                          {sample.gain > 0 ? '+' : ''}{sample.gain.toFixed(2)}%
                        </span>
                      </td>
                      <td className="px-6 py-4 text-zinc-500 text-right font-mono text-xs">
                        {(sample.weight * 100).toFixed(0)}%
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      <div className="bg-[#121214] border border-white/5 rounded-2xl p-6 mt-8 flex flex-col sm:flex-row items-start sm:items-center gap-4">
        <div className="flex-shrink-0 bg-white/5 p-3 rounded-full">
          <AlertTriangle className="h-6 w-6 text-zinc-500" aria-hidden="true" />
        </div>
        <div>
          <h3 className="text-xs uppercase tracking-widest text-zinc-500 font-bold mb-2">Disclaimer</h3>
          <p className="text-xs text-zinc-600 italic leading-tight">
            This prediction is based entirely on historical subscription data using a Gaussian kernel weighting model. 
            It does not account for market sentiment, financials, GMP, sector performance, or broader economic conditions. 
            Past performance is not indicative of future results. This tool should not be construed as financial advice.
          </p>
        </div>
      </div>
    </div>
  );
}
