import { useState, type FormEvent } from 'react';
import { Chip, Head, Modal, Notice, Panel, Row, Stat, Unavailable } from '../components/ui';
import { LiquidCard } from '../components/LiquidSurface';
import { field, formatDate, muted, outline, solid } from '../components/styles';
import {
  formatInr,
  instruments,
  loadFills,
  makeFill,
  paperFee,
  paperQuotes,
  replayFills,
  startingCash,
  storageKey,
  type Fill,
  type Instrument,
  type Ticket,
} from './paperLedger';

const tabs = ['Portfolio', 'Paper order', 'Orders', 'Strategies', 'Backtests'] as const;
type Tab = (typeof tabs)[number];
function readSandbox() {
  try {
    return { fills: loadFills(localStorage), storageError: '' };
  } catch {
    return {
      fills: [] as Fill[],
      storageError:
        'The saved paper ledger could not be read. Reset or place an order to start a new local ledger.',
    };
  }
}

function EquityChart({ series }: { series: number[] }) {
  if (series.length < 2)
    return (
      <p className={`py-9 text-center ${muted}`}>
        Place a paper order to chart virtual equity after each fill.
      </p>
    );
  const values = series.slice(-24);
  const lower = Math.min(...values);
  const range = Math.max(...values) - lower || 1;
  const coordinates = values
    .map(
      (value, index) =>
        `${8 + (index * 284) / (values.length - 1)},${84 - ((value - lower) * 68) / range}`
    )
    .join(' ');
  return (
    <figure className="mt-3">
      <svg
        role="img"
        aria-label={`Simulated equity fell from ${formatInr(values[0])} to ${formatInr(values.at(-1) ?? startingCash)} over recent local fills`}
        viewBox="0 0 300 96"
        className="h-28 w-full"
      >
        <path
          d="M8 16H292 M8 50H292 M8 84H292"
          fill="none"
          stroke="#44454c"
          strokeDasharray="3 5"
        />
        <polyline
          points={coordinates}
          fill="none"
          stroke="#d6d7dd"
          strokeWidth="2"
          strokeLinejoin="round"
        />
      </svg>
      <figcaption className={muted}>
        Virtual equity after recent fills. With fixed fictional quotes, changes reflect simulated
        fees only.
      </figcaption>
    </figure>
  );
}

export default function TradingLab() {
  const [sandbox, setSandbox] = useState(readSandbox);
  const [tab, setTab] = useState<Tab>('Portfolio');
  const [instrument, setInstrument] = useState<Instrument>('NIFTYBEES');
  const [side, setSide] = useState<Ticket['side']>('Buy');
  const [quantity, setQuantity] = useState('5');
  const [feedback, setFeedback] = useState<{ text: string; error?: boolean }>();
  const [resetting, setResetting] = useState(false);
  const ledger = replayFills(sandbox.fills);
  const equityChange = ledger.equity - startingCash;

  function keep(fills: Fill[]) {
    try {
      if (fills.length) localStorage.setItem(storageKey, JSON.stringify(fills));
      else localStorage.removeItem(storageKey);
      setSandbox({ fills, storageError: '' });
    } catch {
      setSandbox({
        fills,
        storageError:
          'Browser storage is unavailable. These changes apply only while this page is open.',
      });
    }
  }
  function fill(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      const order = makeFill({ instrument, side, quantity: Number(quantity) }, sandbox.fills);
      keep([...sandbox.fills, order]);
      setTab('Orders');
      setFeedback({ text: 'Paper order recorded locally. No broker order was placed.' });
    } catch (error) {
      setFeedback({
        text: error instanceof Error ? error.message : 'The paper order could not be recorded.',
        error: true,
      });
    }
  }

  return (
    <>
      <Head
        label="TRADING LAB / NO REAL MONEY"
        title="Trading Lab"
        desc="Try a browser-local paper ledger at fixed fictional prices. Market data and brokerage are unavailable."
        action={<Chip>SIMULATION ONLY</Chip>}
      />
      <nav
        aria-label="Trading views"
        className="mb-6 flex flex-wrap gap-x-5 gap-y-2 border-b border-white/10"
      >
        {tabs.map(name => (
          <LiquidCard
            key={name}
            aria-pressed={tab === name}
            onClick={() => {
              setFeedback(undefined);
              setTab(name);
            }}
            className={`border-b-2 pb-3 text-xs ${tab === name ? 'border-white text-white' : 'border-transparent text-[#a4a5ad]'}`}
          >
            {name}
          </LiquidCard>
        ))}
      </nav>
      {sandbox.storageError && (
        <div className="mb-4">
          <Notice error>{sandbox.storageError}</Notice>
        </div>
      )}
      {feedback && (
        <div className="mb-4">
          <Notice error={feedback.error}>{feedback.text}</Notice>
        </div>
      )}
      {tab === 'Portfolio' && (
        <>
          <div className="grid grid-cols-3 gap-3 max-lg:grid-cols-1">
            <Stat
              label="VIRTUAL EQUITY"
              value={formatInr(ledger.equity)}
              detail="Marked at fixed fictional prices"
            />
            <Stat
              label="AVAILABLE CASH"
              value={formatInr(ledger.cash)}
              detail="Virtual funds only"
            />
            <Stat
              label="SIMULATED P&L"
              value={formatInr(equityChange)}
              detail="Change from starting equity, including fees"
            />
          </div>
          <div className="mt-5 grid grid-cols-2 gap-4 max-lg:grid-cols-1">
            <Panel cls="p-6">
              <h2 className="font-semibold">Paper positions</h2>
              {instruments
                .filter(symbol => ledger.units[symbol] > 0)
                .map(symbol => (
                  <Row
                    key={symbol}
                    main={symbol}
                    sub={`${ledger.units[symbol]} simulated units`}
                    right={formatInr(ledger.units[symbol] * paperQuotes[symbol])}
                  />
                ))}
              {instruments.every(symbol => ledger.units[symbol] === 0) && (
                <p className={`py-10 text-center ${muted}`}>
                  No positions. Place a paper buy to see it here.
                </p>
              )}
            </Panel>
            <Panel cls="p-6">
              <h2 className="font-semibold">Portfolio research</h2>
              <EquityChart series={[startingCash, ...ledger.entries.map(entry => entry.equity)]} />
              <button
                type="button"
                className={solid + ' mt-4'}
                onClick={() => setTab('Paper order')}
              >
                New paper order
              </button>
            </Panel>
          </div>
        </>
      )}
      {tab === 'Paper order' && (
        <div className="grid max-w-[890px] grid-cols-2 gap-4 max-lg:grid-cols-1">
          <Panel cls="p-6">
            <h2 className="font-semibold">Simulated order ticket</h2>
            <form className="mt-5 space-y-4" onSubmit={fill}>
              <label className="block text-xs">
                Instrument
                <select
                  className={field + ' mt-2'}
                  value={instrument}
                  onChange={event => {
                    const selected = instruments.find(symbol => symbol === event.target.value);
                    if (selected) setInstrument(selected);
                  }}
                >
                  {instruments.map(symbol => (
                    <option key={symbol}>{symbol}</option>
                  ))}
                </select>
              </label>
              <label className="block text-xs">
                Order side
                <select
                  className={field + ' mt-2'}
                  value={side}
                  onChange={event => setSide(event.target.value === 'Sell' ? 'Sell' : 'Buy')}
                >
                  <option>Buy</option>
                  <option>Sell</option>
                </select>
              </label>
              <label className="block text-xs">
                Units
                <input
                  className={field + ' mt-2'}
                  required
                  type="number"
                  min="1"
                  max="500"
                  step="1"
                  value={quantity}
                  onChange={event => setQuantity(event.target.value)}
                />
              </label>
              <button type="submit" className={solid + ' w-full'}>
                Simulate {side.toLowerCase()}
              </button>
            </form>
          </Panel>
          <Panel cls="h-fit p-6">
            <h2 className="font-semibold">Execution preview</h2>
            <Row
              main="Reference price"
              sub="Fixed, fictional quote"
              right={formatInr(paperQuotes[instrument])}
            />
            <Row
              main="Gross total"
              sub="Before simulated fees"
              right={
                Number(quantity) >= 1 && Number(quantity) <= 500
                  ? formatInr(Number(quantity) * paperQuotes[instrument])
                  : '—'
              }
            />
            <Row main="Example charge" sub="Not official brokerage" right={formatInr(paperFee)} />
            <Row main="Virtual cash" sub="Your simulation balance" right={formatInr(ledger.cash)} />
            <p className={`mt-4 ${muted}`}>
              No live, delayed or historical market feed is connected. Nothing is sent to Paytm
              Money.
            </p>
          </Panel>
        </div>
      )}
      {tab === 'Orders' && (
        <Panel cls="max-w-[950px] p-6">
          <h2 className="font-semibold">Simulated fills &amp; virtual cash ledger</h2>
          {!ledger.entries.length && (
            <p className={`py-10 text-center ${muted}`}>No paper trades yet.</p>
          )}
          {ledger.entries
            .slice()
            .reverse()
            .map(({ fill: order, cash, delta }) => (
              <Row
                key={order.id}
                main={`${order.side} ${order.quantity} ${order.instrument}`}
                sub={`${formatDate(order.time)} · ${formatInr(paperQuotes[order.instrument])}/unit`}
                right={
                  <div className="text-right">
                    <b>
                      {delta > 0 ? '+' : ''}
                      {formatInr(delta)}
                    </b>
                    <p className="mt-1">Cash: {formatInr(cash)}</p>
                  </div>
                }
              />
            ))}
        </Panel>
      )}
      {(tab === 'Strategies' || tab === 'Backtests') && (
        <Unavailable
          title={tab}
          reason="The browser API does not provide a strategy library, historical data, backtest runs, risk analytics, watchlists or a server trading journal. No historical returns are fabricated."
        >
          <button type="button" disabled className={outline}>
            {tab === 'Strategies' ? 'Save strategy' : 'Run backtest'}
          </button>
        </Unavailable>
      )}
      <div className="mt-5 flex flex-wrap items-center justify-between gap-3">
        <p className={muted}>
          This fictional ledger is stored in this browser only. It is not part of your account.
        </p>
        <button
          type="button"
          className={outline}
          disabled={!sandbox.fills.length && !sandbox.storageError}
          onClick={() => setResetting(true)}
        >
          Reset paper sandbox
        </button>
      </div>
      {resetting && (
        <Modal title="Reset paper sandbox" close={() => setResetting(false)}>
          <p className={muted}>
            Clear this browser’s simulated orders and restore the starting virtual cash? This does
            not affect server data.
          </p>
          <button
            type="button"
            className={solid + ' mt-4'}
            onClick={() => {
              keep([]);
              setResetting(false);
              setFeedback({ text: 'The displayed paper ledger has been cleared.' });
            }}
          >
            Clear local paper ledger
          </button>
        </Modal>
      )}
    </>
  );
}
