import type { Candle } from './marketData';

export function sma(values: number[], period: number): Array<number | null> {
  if (period < 1) throw new Error('SMA period must be positive.');
  let sum = 0;
  return values.map((value, index) => {
    sum += value;
    if (index >= period) sum -= values[index - period];
    return index >= period - 1 ? sum / period : null;
  });
}

export function ema(values: number[], period: number): Array<number | null> {
  if (period < 1) throw new Error('EMA period must be positive.');
  if (values.length < period) return values.map(() => null);
  const multiplier = 2 / (period + 1);
  const output: Array<number | null> = values.map(() => null);
  let previous = values.slice(0, period).reduce((total, value) => total + value, 0) / period;
  output[period - 1] = previous;
  for (let index = period; index < values.length; index++)
    output[index] = previous = (values[index] - previous) * multiplier + previous;
  return output;
}

export function rsi(values: number[], period = 14): Array<number | null> {
  if (period < 1 || values.length < period + 1) return values.map(() => null);
  const gains: number[] = [];
  const losses: number[] = [];
  for (let index = 1; index < values.length; index++) {
    const delta = values[index] - values[index - 1];
    gains.push(Math.max(delta, 0));
    losses.push(Math.max(-delta, 0));
  }
  let gain = gains.slice(0, period).reduce((total, value) => total + value, 0) / period;
  let loss = losses.slice(0, period).reduce((total, value) => total + value, 0) / period;
  const output: Array<number | null> = values.map(() => null);
  const at = (averageGain: number, averageLoss: number) =>
    averageLoss === 0 && averageGain === 0
      ? 50
      : averageLoss === 0
        ? 100
        : averageGain === 0
          ? 0
          : 100 - 100 / (1 + averageGain / averageLoss);
  output[period] = at(gain, loss);
  for (let index = period; index < gains.length; index++) {
    gain = (gain * (period - 1) + gains[index]) / period;
    loss = (loss * (period - 1) + losses[index]) / period;
    output[index + 1] = at(gain, loss);
  }
  return output;
}

export function macd(values: number[], fast = 12, slow = 26, signalPeriod = 9) {
  if (fast >= slow) throw new Error('MACD fast period must be shorter than slow period.');
  const fastLine = ema(values, fast);
  const slowLine = ema(values, slow);
  const line = values.map((_, index) =>
    fastLine[index] == null || slowLine[index] == null ? null : fastLine[index]! - slowLine[index]!
  );
  const complete = line.filter((value): value is number => value !== null);
  const signalComplete = ema(complete, signalPeriod);
  const signal: Array<number | null> = line.map(() => null);
  let cursor = 0;
  line.forEach((value, index) => {
    if (value !== null) signal[index] = signalComplete[cursor++];
  });
  const histogram = line.map((value, index) =>
    value == null || signal[index] == null ? null : value - signal[index]!
  );
  return { line, signal, histogram };
}

export function volumeAverage(candles: Candle[], period = 20) {
  return sma(
    candles.map(candle => candle.volume),
    period
  );
}

export type MarketSignal = {
  name: string;
  bias: 'Bullish' | 'Bearish' | 'Neutral';
  rule: string;
  value: string;
  timestamp: number;
};

export function signals(candles: Candle[]): MarketSignal[] {
  if (!candles.length) return [];
  const closes = candles.map(candle => candle.close);
  const latest = candles.at(-1)!;
  const sma20 = sma(closes, 20);
  const sma50 = sma(closes, 50);
  const rsi14 = rsi(closes);
  const macdValue = macd(closes);
  const last = closes.length - 1;
  const cross =
    sma20[last] == null || sma50[last] == null
      ? 'Neutral'
      : sma20[last]! > sma50[last]!
        ? 'Bullish'
        : sma20[last]! < sma50[last]!
          ? 'Bearish'
          : 'Neutral';
  const latestRsi = rsi14[last];
  const rsiBias =
    latestRsi == null
      ? 'Neutral'
      : latestRsi > 70
        ? 'Bearish'
        : latestRsi < 30
          ? 'Bullish'
          : 'Neutral';
  const currentMacd = macdValue.line[last];
  const signal = macdValue.signal[last];
  return [
    {
      name: 'Moving-average trend',
      bias: cross,
      rule: 'SMA 20 compared with SMA 50. Equal values are neutral.',
      value: `SMA20 ${sma20[last]?.toFixed(2) ?? 'warming up'} · SMA50 ${sma50[last]?.toFixed(2) ?? 'warming up'}`,
      timestamp: latest.time,
    },
    {
      name: 'RSI range',
      bias: rsiBias,
      rule: 'RSI above 70 is marked overbought; below 30 is marked oversold.',
      value: latestRsi == null ? 'Warming up' : latestRsi.toFixed(2),
      timestamp: latest.time,
    },
    {
      name: 'MACD confirmation',
      bias:
        currentMacd == null || signal == null
          ? 'Neutral'
          : currentMacd > signal
            ? 'Bullish'
            : currentMacd < signal
              ? 'Bearish'
              : 'Neutral',
      rule: 'MACD line (12/26 EMA) compared with its 9-period signal.',
      value:
        currentMacd == null || signal == null
          ? 'Warming up'
          : `${currentMacd.toFixed(2)} vs ${signal.toFixed(2)}`,
      timestamp: latest.time,
    },
  ];
}
