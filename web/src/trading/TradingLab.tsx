import { useMemo, useRef, useState, type FormEvent } from 'react';
import { Chip, Head, Modal, Notice, Panel, Row } from '../components/ui';
import { field, formatDate, muted, outline, solid } from '../components/styles';
import { MarketChart } from './MarketChart';
import {
  demoInstruments,
  demoProvider,
  missingMarketData,
  type MarketInstrument,
  type MarketInterval,
} from './marketData';
import { signals, volumeAverage } from './indicators';
import {
  cancelOrder,
  formatInr,
  freshLedger,
  loadLedger,
  paperFee,
  refreshLimitOrders,
  replayLedger,
  saveLedger,
  startingCash,
  storageKey,
  submitPaperOrder,
  type Instrument,
  type PaperLedger,
} from './paperLedger';

function readLedger() {
  try {
    return { ledger: loadLedger(localStorage), error: '' };
  } catch {
    return {
      ledger: freshLedger(),
      error: 'The saved paper ledger could not be read. Reset it before continuing.',
    };
  }
}

function EquityLine({ values }: { values: number[] }) {
  if (values.length < 2)
    return <p className={`py-8 text-center ${muted}`}>No fill history to chart yet.</p>;
  const lower = Math.min(...values);
  const range = Math.max(...values) - lower || 1;
  return (
    <figure>
      <svg
        viewBox="0 0 300 100"
        role="img"
        aria-label={`Virtual equity from ${formatInr(values[0])} to ${formatInr(values.at(-1)!)}`}
        className="h-28 w-full"
      >
        <polyline
          points={values
            .map(
              (value, index) =>
                `${6 + (index * 288) / (values.length - 1)},${88 - ((value - lower) * 74) / range}`
            )
            .join(' ')}
          fill="none"
          stroke="#d8d9de"
          strokeWidth="2"
          strokeLinejoin="round"
        />
      </svg>
      <figcaption className={muted}>
        Equity reconstructed from saved fills and their execution snapshots.
      </figcaption>
    </figure>
  );
}

export default function TradingLab() {
  const [instrument, setInstrument] = useState<MarketInstrument>(demoInstruments[0]);
  const [interval, setInterval] = useState<MarketInterval>('1d');
  const [data, setData] = useState(() => missingMarketData(demoInstruments[0], '1d'));
  const [demo, setDemo] = useState(false);
  const [loading, setLoading] = useState(false);
  const [search, setSearch] = useState('');
  const [side, setSide] = useState<'Buy' | 'Sell'>('Buy');
  const [orderType, setOrderType] = useState<'Market' | 'Limit'>('Market');
  const [quantity, setQuantity] = useState('1');
  const [limit, setLimit] = useState('');
  const [showSma, setShowSma] = useState(true);
  const [showEma, setShowEma] = useState(false);
  const [store, setStore] = useState(readLedger);
  const [feedback, setFeedback] = useState<{ text: string; error?: boolean }>();
  const [resetting, setResetting] = useState(false);
  const request = useRef(0);
  const latest = data.candles.at(-1);
  const mark = latest ? Math.round(latest.close * 100) : undefined;
  const marks = mark ? { [instrument.symbol as Instrument]: mark } : {};
  const portfolio = replayLedger(store.ledger, marks);
  const openOrders = store.ledger.orders.filter(order => order.status === 'Open');
  const marketSignals = useMemo(() => signals(data.candles), [data.candles]);
  const filtered = demoInstruments.filter(item =>
    `${item.symbol} ${item.name}`.toLowerCase().includes(search.toLowerCase())
  );
  const volumeTrend = volumeAverage(data.candles);
  const latestAverageVolume = volumeTrend.at(-1);

  async function choose(next: MarketInstrument, nextInterval = interval, explicitDemo = demo) {
    setInstrument(next);
    setInterval(nextInterval);
    if (!explicitDemo) {
      setData(missingMarketData(next, nextInterval));
      return;
    }
    const id = ++request.current;
    setLoading(true);
    try {
      const series = await demoProvider.candles(next, nextInterval);
      if (id !== request.current) return;
      const refreshed = refreshLimitOrders(store.ledger, series);
      if (refreshed !== store.ledger) persist(refreshed);
      setData(series);
    } finally {
      if (id === request.current) setLoading(false);
    }
  }

  function persist(ledger: PaperLedger) {
    try {
      saveLedger(localStorage, ledger);
      setStore({ ledger, error: '' });
    } catch {
      setStore({
        ledger,
        error: 'Browser storage is unavailable. This ledger lasts only for this visit.',
      });
    }
  }

  function place(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      const next = submitPaperOrder(
        store.ledger,
        {
          instrument: instrument.symbol as Instrument,
          side,
          type: orderType,
          quantity: Number(quantity),
          limit: orderType === 'Limit' ? Number(limit) : undefined,
        },
        data
      );
      persist(next);
      setFeedback({
        text:
          next.orders.at(-1)?.status === 'Open'
            ? 'Limit order saved as open. No broker order was placed.'
            : 'Paper fill recorded locally. No broker order was placed.',
      });
    } catch (error) {
      setFeedback({
        text: error instanceof Error ? error.message : 'The paper order was rejected.',
        error: true,
      });
    }
  }

  function equityValues() {
    return [startingCash, ...portfolio.entries.map(entry => entry.equity)];
  }

  return (
    <>
      <Head
        label="RESEARCH / PAPER TRADING"
        title="Trading Lab"
        desc="Inspect price history, transparent signals and a virtual ledger. No real-money execution is available."
        action={
          <Chip>
            {data.available
              ? data.mode === 'demo'
                ? 'SYNTHETIC DEMO'
                : data.mode.toUpperCase()
              : 'OFFLINE'}
          </Chip>
        }
      />
      {store.error && (
        <div className="mb-4">
          <Notice error>{store.error}</Notice>
        </div>
      )}
      {feedback && (
        <div className="mb-4">
          <Notice error={feedback.error}>{feedback.text}</Notice>
        </div>
      )}
      <div className="th-trading-layout">
        <aside className="th-trading-watchlist" aria-label="Instrument search and watchlist">
          <Panel cls="p-4">
            <label className="block text-xs">
              Instrument search
              <input
                className={`${field} mt-2`}
                type="search"
                value={search}
                onChange={event => setSearch(event.target.value)}
                placeholder="Symbol or name"
              />
            </label>
            <ul className="mt-3">
              {filtered.map(item => (
                <li key={item.symbol}>
                  <button
                    className={`w-full rounded-lg px-3 py-3 text-left hover:bg-white/5 ${instrument.symbol === item.symbol ? 'bg-white/10' : ''}`}
                    type="button"
                    aria-pressed={instrument.symbol === item.symbol}
                    onClick={() => void choose(item)}
                  >
                    <b className="text-xs">{item.symbol}</b>
                    <span className={`mt-1 block ${muted}`}>
                      {item.exchange} ·{' '}
                      {data.available && item.symbol === instrument.symbol
                        ? latest!.close.toFixed(2)
                        : 'No quote loaded'}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
            {!demo && (
              <div className="mt-3 border-t border-white/10 pt-3">
                <p className={muted}>No market-data contract is available in Web v1.</p>
                <button
                  type="button"
                  className={`${outline} mt-3 w-full`}
                  onClick={() => {
                    setDemo(true);
                    void choose(instrument, interval, true);
                  }}
                >
                  Load synthetic demo
                </button>
              </div>
            )}
            {demo && (
              <button
                type="button"
                className={`${outline} mt-3 w-full`}
                onClick={() => {
                  setDemo(false);
                  setData(missingMarketData(instrument, interval));
                }}
              >
                Return to offline
              </button>
            )}
          </Panel>
        </aside>

        <section className="min-w-0" aria-label="Market research">
          <Panel cls="p-5">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <h2 className="text-lg font-semibold">{instrument.symbol}</h2>
                <p className={muted}>
                  {instrument.name} · {instrument.exchange}
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {(['15m', '1h', '1d'] as const).map(value => (
                  <button
                    key={value}
                    type="button"
                    className={outline}
                    aria-pressed={interval === value}
                    onClick={() => void choose(instrument, value)}
                    disabled={loading}
                  >
                    {value}
                  </button>
                ))}
              </div>
            </div>
            <div className="mt-4 flex flex-wrap gap-x-4 gap-y-2 text-xs text-[#c4c5cb]">
              <span>Source: {data.source}</span>
              <span>Exchange: {instrument.exchange}</span>
              <span>
                Delay:{' '}
                {data.mode === 'demo'
                  ? 'Synthetic / not market data'
                  : data.available
                    ? data.mode
                    : 'Unavailable'}
              </span>
              <span>Market state: unavailable</span>
              <span>Zone: {data.timezone}</span>
              <span>Updated: {data.available ? formatDate(data.updatedAt) : 'Never'}</span>
            </div>
            <div className="mt-4 flex flex-wrap gap-4 text-xs">
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={showSma}
                  onChange={event => setShowSma(event.target.checked)}
                />{' '}
                SMA 20
              </label>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={showEma}
                  onChange={event => setShowEma(event.target.checked)}
                />{' '}
                EMA 12
              </label>
            </div>
            <div className="mt-3" aria-busy={loading}>
              {loading && (
                <p role="status" className={muted}>
                  Generating the explicitly selected synthetic series…
                </p>
              )}
              <MarketChart data={data} showSma={showSma} showEma={showEma} />
            </div>
          </Panel>
        </section>

        <aside aria-label="Paper order ticket">
          <Panel cls="p-4">
            <h2 className="font-semibold">Paper order</h2>
            <form className="mt-4 space-y-3" onSubmit={place}>
              <div className="grid grid-cols-2 gap-2">
                {(['Buy', 'Sell'] as const).map(value => (
                  <button
                    key={value}
                    type="button"
                    className={`${outline} ${side === value ? 'bg-white/15' : ''}`}
                    aria-pressed={side === value}
                    onClick={() => setSide(value)}
                  >
                    {value}
                  </button>
                ))}
              </div>
              <label className="block text-xs">
                Order type
                <select
                  className={`${field} mt-2`}
                  value={orderType}
                  onChange={event =>
                    setOrderType(event.target.value === 'Limit' ? 'Limit' : 'Market')
                  }
                >
                  <option>Market</option>
                  <option>Limit</option>
                </select>
              </label>
              <label className="block text-xs">
                Quantity
                <input
                  required
                  className={`${field} mt-2`}
                  type="number"
                  min="1"
                  max="10000"
                  step="1"
                  value={quantity}
                  onChange={event => setQuantity(event.target.value)}
                />
              </label>
              {orderType === 'Limit' && (
                <label className="block text-xs">
                  Limit price (INR)
                  <input
                    required
                    className={`${field} mt-2`}
                    type="number"
                    min="0.01"
                    step="0.01"
                    value={limit}
                    onChange={event => setLimit(event.target.value)}
                  />
                </label>
              )}
              <Row
                main="Selected mark"
                sub={data.available ? data.source : 'Offline'}
                right={latest ? latest.close.toFixed(2) : '—'}
              />
              <Row main="Paper fee" sub="Assumption per fill" right={formatInr(paperFee)} />
              <button type="submit" className={`${solid} w-full`} disabled={!data.available}>
                Place virtual {side.toLowerCase()}
              </button>
            </form>
          </Panel>
        </aside>
      </div>

      <div className="mt-4 grid grid-cols-4 gap-3 max-xl:grid-cols-2 max-md:grid-cols-1">
        <Panel cls="p-4">
          <p className={muted}>Virtual equity</p>
          <p className="mt-2 text-xl font-semibold">{formatInr(portfolio.equity)}</p>
          {!portfolio.fullyMarked && (
            <p className={`mt-2 ${muted}`}>
              Cost-basis fallback used where a current mark is unavailable.
            </p>
          )}
        </Panel>
        <Panel cls="p-4">
          <p className={muted}>Virtual cash</p>
          <p className="mt-2 text-xl font-semibold">{formatInr(portfolio.cash)}</p>
        </Panel>
        <Panel cls="p-4">
          <p className={muted}>Invested / market value</p>
          <p className="mt-2 text-xl font-semibold">
            {formatInr(portfolio.invested)} / {formatInr(portfolio.marketValue)}
          </p>
        </Panel>
        <Panel cls="p-4">
          <p className={muted}>Realized / unrealized</p>
          <p className="mt-2 text-xl font-semibold">
            {formatInr(portfolio.realized)} / {formatInr(portfolio.unrealized)}
          </p>
        </Panel>
      </div>

      <div className="mt-4 grid grid-cols-2 gap-4 max-lg:grid-cols-1">
        <Panel cls="p-5">
          <h2 className="font-semibold">Positions & allocation</h2>
          {!portfolio.positions.length ? (
            <p className={`py-8 text-center ${muted}`}>No simulated holdings.</p>
          ) : (
            portfolio.positions.map(position => (
              <Row
                key={position.instrument}
                main={`${position.instrument} · ${position.quantity} units`}
                sub={`Avg ${formatInr(position.averageCost)} · ${position.markAvailable ? `Mark ${formatInr(position.mark)}` : 'No current mark; cost-basis fallback'}`}
                right={`${formatInr(position.unrealized)} · ${(((position.quantity * position.mark) / portfolio.equity) * 100).toFixed(1)}%`}
              />
            ))
          )}
        </Panel>
        <Panel cls="p-5">
          <h2 className="font-semibold">Persisted paper equity</h2>
          <EquityLine values={equityValues()} />
        </Panel>
      </div>

      <div className="mt-4 grid grid-cols-2 gap-4 max-lg:grid-cols-1">
        <Panel cls="p-5">
          <h2 className="font-semibold">Transparent signals</h2>
          {!data.available || !marketSignals.length ? (
            <p className={`py-8 text-center ${muted}`}>
              Signals require explicitly loaded candle data.
            </p>
          ) : (
            marketSignals.map(signal => (
              <Row
                key={signal.name}
                main={`${signal.name} · ${signal.bias}`}
                sub={`${signal.rule} ${signal.value} · ${formatDate(new Date(signal.timestamp * 1000).toISOString())} · ${data.source} · ${interval}`}
              />
            ))
          )}
          <p className={`mt-3 ${muted}`}>
            {latestAverageVolume == null
              ? 'Volume average is warming up.'
              : `Volume SMA 20: ${Math.round(latestAverageVolume).toLocaleString('en-IN')}`}
          </p>
          <p className={`mt-2 ${muted}`}>
            These deterministic rules describe the loaded series. They do not predict returns or
            guarantee profitability.
          </p>
        </Panel>
        <Panel cls="p-5">
          <h2 className="font-semibold">Open paper orders</h2>
          {!openOrders.length ? (
            <p className={`py-8 text-center ${muted}`}>No open orders.</p>
          ) : (
            openOrders.map(order => (
              <Row
                key={order.id}
                main={`${order.side} ${order.quantity} ${order.instrument}`}
                sub={`${order.type} · limit ${order.limit ?? '—'} · ${order.source}`}
                right={
                  <button
                    type="button"
                    className={outline}
                    onClick={() => {
                      try {
                        persist(cancelOrder(store.ledger, order.id));
                        setFeedback({ text: 'Open paper order cancelled locally.' });
                      } catch (error) {
                        setFeedback({
                          text: error instanceof Error ? error.message : 'Could not cancel order.',
                          error: true,
                        });
                      }
                    }}
                  >
                    Cancel
                  </button>
                }
              />
            ))
          )}
        </Panel>
      </div>

      <Panel cls="mt-4 p-5">
        <h2 className="font-semibold">Orders & simulated trade ledger</h2>
        {!store.ledger.orders.length ? (
          <p className={`py-8 text-center ${muted}`}>No paper orders yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="mt-3 w-full min-w-[720px] text-left text-xs">
              <thead>
                <tr className="border-b border-white/10 text-[#a4a5af]">
                  <th className="py-3">Time</th>
                  <th>Instrument</th>
                  <th>Side / type</th>
                  <th>Qty</th>
                  <th>Fill</th>
                  <th>Fee</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {store.ledger.orders
                  .slice()
                  .reverse()
                  .map(order => (
                    <tr key={order.id} className="border-b border-white/[.07]">
                      <td className="py-3">{formatDate(order.filledAt ?? order.submittedAt)}</td>
                      <td>{order.instrument}</td>
                      <td>
                        {order.side} / {order.type}
                      </td>
                      <td>{order.quantity}</td>
                      <td>{order.fillPrice == null ? '—' : formatInr(order.fillPrice)}</td>
                      <td>{formatInr(order.fees)}</td>
                      <td>{order.status}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
        <p className={muted}>
          Virtual funds only. This ledger is stored in this browser and can be cleared by the
          browser. Backend persistence and provider feeds are not contracted.
        </p>
        <button
          type="button"
          className={outline}
          disabled={!store.ledger.orders.length && !store.error}
          onClick={() => setResetting(true)}
        >
          Reset paper ledger
        </button>
      </div>
      {resetting && (
        <Modal title="Reset paper ledger?" close={() => setResetting(false)}>
          <p className={muted}>
            Clear local virtual orders and restore ₹100,000 of starting cash? No server or brokerage
            data is affected.
          </p>
          <button
            className={`${solid} mt-4`}
            type="button"
            onClick={() => {
              persist(freshLedger());
              localStorage.removeItem(storageKey);
              setResetting(false);
              setFeedback({ text: 'Local paper ledger reset.' });
            }}
          >
            Reset local ledger
          </button>
        </Modal>
      )}
    </>
  );
}
