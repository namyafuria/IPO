import { useEffect, useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import { getIpoByName } from '../lib/api';
import { ArrowLeft, Info, RefreshCw } from 'lucide-react';
import axios from 'axios';

export default function IPODetail() {
  const { name } = useParams<{ name: string }>();
  const [ipo, setIpo] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fetchMessage, setFetchMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!name) return;
    
    getIpoByName(name).then(data => {
      setIpo(data);
      setLoading(false);
    }).catch(err => {
      console.error(err);
      setError('Failed to load IPO details.');
      setLoading(false);
    });
  }, [name]);

  const handleFetchMissing = async () => {
    if (!name) return;
    setRefreshing(true);
    try {
      const response = await axios.get(`/api/fetch-missing?name=${encodeURIComponent(name)}`);
      setIpo(response.data);
    } catch (err) {
      console.error(err);
    } finally {
      setRefreshing(false);
    }
  };

  if (loading) {
    return (
      <div className="flex justify-center items-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-emerald-500"></div>
      </div>
    );
  }

  if (error || !ipo) {
    return (
      <div className="max-w-3xl mx-auto px-4 py-12 text-center">
        <p className="text-rose-500 mb-4">{error || 'IPO not found'}</p>
        <Link to="/ipos" className="text-emerald-400 hover:underline">Return to IPO list</Link>
      </div>
    );
  }

  const renderField = (label: string, value: any, format?: (v: any) => string) => {
    let displayValue = value !== null && value !== undefined ? (format ? format(value) : value) : '—';
    return (
      <div className="py-4 sm:py-5 sm:grid sm:grid-cols-3 sm:gap-4 sm:px-6">
        <dt className="text-sm text-zinc-500">{label}</dt>
        <dd className="mt-1 text-sm text-zinc-300 font-mono sm:mt-0 sm:col-span-2">{displayValue}</dd>
      </div>
    );
  };

  const hasMissingFields = [
    ipo.subscription_total,
    ipo.issue_size_cr,
    ipo.issue_price,
    ipo.sector
  ].some(field => field === null || field === undefined);

  return (
    <div className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8 animate-in fade-in duration-500">
      <Link to="/ipos" className="inline-flex items-center space-x-2 text-sm text-emerald-400 hover:text-emerald-300 mb-6 transition-colors">
        <ArrowLeft className="w-4 h-4" />
        <span>Back to IPOs</span>
      </Link>
      
      <div className="bg-[#121214] shadow-2xl overflow-hidden rounded-2xl border border-white/5">
        <div className="px-6 py-6 border-b border-white/5 flex flex-col md:flex-row md:justify-between md:items-center bg-white/[0.02]">
          <div>
            <h3 className="text-2xl font-serif italic text-zinc-200 mb-1">{ipo.name}</h3>
            <div className="flex items-center gap-4">
              <p className="text-xs uppercase tracking-widest text-zinc-500 font-bold">{ipo.sector || 'Sector not specified'}</p>
              {hasMissingFields && (
                <button
                  onClick={handleFetchMissing}
                  disabled={refreshing}
                  className="flex items-center gap-1 text-[9px] uppercase tracking-widest font-bold text-emerald-400 hover:text-emerald-300 disabled:opacity-50 transition-colors bg-emerald-500/10 px-2 py-0.5 rounded border border-emerald-500/20"
                >
                  <RefreshCw className={`w-3 h-3 ${refreshing ? 'animate-spin' : ''}`} />
                  {refreshing ? 'Fetching...' : 'Fetch Missing Data'}
                </button>
              )}
            </div>
          </div>
          {ipo.listing_gain_pct !== null ? (
            <div className={`mt-4 md:mt-0 px-4 py-2 rounded-lg font-bold text-lg font-mono border ${ipo.listing_gain_pct > 0 ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20' : ipo.listing_gain_pct < 0 ? 'bg-rose-500/10 text-rose-400 border-rose-500/20' : 'bg-white/5 text-zinc-300 border-white/10'}`}>
              {ipo.listing_gain_pct > 0 ? '+' : ''}{ipo.listing_gain_pct.toFixed(2)}%
            </div>
          ) : (
            <div className="mt-4 md:mt-0 px-4 py-2 rounded-lg font-bold text-xs uppercase tracking-widest font-sans border bg-amber-500/10 text-amber-400 border-amber-500/20">
              Not yet listed
            </div>
          )}
        </div>
        <div className="px-6 py-6 sm:p-0">
          <dl className="sm:divide-y sm:divide-white/5">
            {renderField('Listed Date', ipo.listed_date)}
            {renderField('Total Subscription', ipo.subscription_total, (v) => `${v.toFixed(2)}x`)}
            
            <div className="bg-white/[0.02] px-6 py-3 text-xs uppercase tracking-widest text-zinc-500 font-bold border-y border-white/5 mt-4 sm:mt-0">Financials & Details</div>
            {renderField('Issue Price (₹)', ipo.issue_price)}
            {renderField('Issue Size (₹ Cr)', ipo.issue_size_cr)}
            {renderField('P/E Ratio', ipo.pe_ratio)}
            {renderField('RoE (%)', ipo.roe)}
            {renderField('Debt to Equity', ipo.debt_equity)}
            
            <div className="bg-white/[0.02] px-6 py-3 text-xs uppercase tracking-widest text-zinc-500 font-bold border-y border-white/5 mt-4 sm:mt-0">Subscription Breakdown</div>
            {renderField('QIB Subscription', ipo.qib_sub, (v) => `${v}x`)}
            {renderField('HNI Subscription', ipo.hni_sub, (v) => `${v}x`)}
            {renderField('Retail Subscription', ipo.rii_sub, (v) => `${v}x`)}
            {renderField('Anchor Allocation (%)', ipo.anchor_pct, (v) => `${v}%`)}
            {renderField('GMP (%)', ipo.gmp_percent, (v) => `${v}%`)}
          </dl>
        </div>
        
        <div className="bg-black/30 px-6 py-4 border-t border-white/5 flex flex-col gap-4">
          <div className="flex items-start space-x-3">
            <Info className="w-5 h-5 text-zinc-500 mt-0.5 flex-shrink-0" />
            <div className="text-xs text-zinc-500 flex-1">
              <p><strong>Data Source:</strong> {ipo.data_source || 'Unknown'}</p>
              <p className="mt-1"><strong>Gain Basis:</strong> {ipo.gain_basis === 'close' ? 'Based on listing day close price' : ipo.gain_basis === 'open' ? 'Based on opening price' : (ipo.gain_basis || 'Unknown')}</p>
              <p className="text-zinc-600 mt-2 font-mono text-[10px]">
                Last updated: {ipo.last_updated ? new Date(ipo.last_updated).toLocaleString() : 'Unknown'}
              </p>
            </div>
            <button
              onClick={handleFetchMissing}
              disabled={refreshing}
              className="flex items-center gap-2 px-3 py-1.5 rounded-lg border border-white/10 text-xs font-bold uppercase tracking-widest text-zinc-300 hover:text-white hover:bg-white/5 transition-colors disabled:opacity-50"
            >
              <RefreshCw className={`w-3 h-3 ${refreshing ? 'animate-spin' : ''}`} />
              {refreshing ? 'Refreshing...' : 'Refresh this IPO'}
            </button>
          </div>
          
          {ipo.data_source && ipo.data_source.toLowerCase().includes('chittorgarh') ? (
            <div className="flex items-center gap-2 bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 px-3 py-2 rounded text-xs">
              <div className="w-1.5 h-1.5 rounded-full bg-emerald-500"></div>
              <span>Data verified from official sources (Chittorgarh) {ipo.data_fetched_at && `at ${new Date(ipo.data_fetched_at).toLocaleString()}`}</span>
            </div>
          ) : (
            <div className="flex items-center gap-2 bg-amber-500/10 border border-amber-500/20 text-amber-400 px-3 py-2 rounded text-xs">
              <div className="w-1.5 h-1.5 rounded-full bg-amber-500"></div>
              <span>Data may be outdated or unverified – refresh to fetch latest from official source.</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
