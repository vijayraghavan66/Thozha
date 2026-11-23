import { useEffect, useId, useRef, useState } from 'react';
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  createChart,
  type UTCTimestamp,
} from 'lightweight-charts';
import type { Candle, MarketSeries } from './marketData';
import { ema, sma } from './indicators';
import { muted } from '../components/styles';

export function MarketChart({
  data,
  showSma,
  showEma,
}: {
  data: MarketSeries;
  showSma: boolean;
  showEma: boolean;
}) {
  const host = useRef<HTMLDivElement>(null);
  const [point, setPoint] = useState<Candle>();
  const captionId = useId();
  useEffect(() => {
    const element = host.current;
    if (!element || !data.candles.length || !data.available) return;
    const chart = createChart(element, {
      width: element.clientWidth,
      height: 360,
      layout: { background: { type: ColorType.Solid, color: '#15161a' }, textColor: '#9fa1ac' },
      grid: { vertLines: { color: '#292a30' }, horzLines: { color: '#292a30' } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: '#34353c' },
      timeScale: {
        borderColor: '#34353c',
        timeVisible: data.interval !== '1d',
        secondsVisible: false,
      },
      handleScroll: true,
      handleScale: true,
    });
    const candles = data.candles.map(candle => ({ ...candle, time: candle.time as UTCTimestamp }));
    const price = chart.addSeries(CandlestickSeries, {
      upColor: '#d7d8de',
      downColor: '#555761',
      wickUpColor: '#d7d8de',
      wickDownColor: '#555761',
      borderVisible: false,
    });
    price.setData(candles);
    const volume = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: '',
      lastValueVisible: false,
      priceLineVisible: false,
    });
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.78, bottom: 0 } });
    volume.setData(
      candles.map(candle => ({
        time: candle.time,
        value: candle.volume,
        color: candle.close >= candle.open ? '#777a85aa' : '#41434aaa',
      }))
    );
    const closes = data.candles.map(candle => candle.close);
    if (showSma) {
      const line = chart.addSeries(LineSeries, {
        color: '#d3d4da',
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      line.setData(
        sma(closes, 20).flatMap((value, index) =>
          value == null ? [] : [{ time: candles[index].time, value }]
        )
      );
    }
    if (showEma) {
      const line = chart.addSeries(LineSeries, {
        color: '#9b9da7',
        lineWidth: 1,
        lineStyle: 2,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      line.setData(
        ema(closes, 12).flatMap((value, index) =>
          value == null ? [] : [{ time: candles[index].time, value }]
        )
      );
    }
    chart.timeScale().fitContent();
    chart.subscribeCrosshairMove(parameter => {
      if (parameter.time == null) return setPoint(undefined);
      const candle = data.candles.find(item => item.time === parameter.time);
      if (candle) setPoint(candle);
    });
    const resize = new ResizeObserver(([entry]) =>
      chart.applyOptions({ width: entry.contentRect.width })
    );
    resize.observe(element);
    const visible = () => {
      if (document.visibilityState === 'hidden') chart.applyOptions({ height: 1 });
      else chart.applyOptions({ height: 360 });
    };
    document.addEventListener('visibilitychange', visible);
    return () => {
      resize.disconnect();
      document.removeEventListener('visibilitychange', visible);
      chart.remove();
    };
  }, [data, showEma, showSma]);
  if (!data.available || !data.candles.length)
    return (
      <div className="grid h-80 place-items-center rounded-xl border border-dashed border-white/15 px-6 text-center">
        <p className={muted}>
          {data.availabilityReason ?? 'No candles are available for this source.'}
        </p>
      </div>
    );
  const last = point ?? data.candles.at(-1)!;
  return (
    <figure>
      <figcaption id={captionId} className={`mb-2 flex flex-wrap gap-x-4 gap-y-1 ${muted}`}>
        <span>
          {new Date(last.time * 1000).toLocaleString('en-IN', { timeZone: data.timezone })}
        </span>
        <span>O {last.open.toFixed(2)}</span>
        <span>H {last.high.toFixed(2)}</span>
        <span>L {last.low.toFixed(2)}</span>
        <span>C {last.close.toFixed(2)}</span>
        <span>Vol {last.volume.toLocaleString('en-IN')}</span>
      </figcaption>
      <div ref={host} aria-describedby={captionId} className="th-market-chart h-[360px] w-full" />
      <p className="sr-only">
        {data.candles.length} {data.interval} candles for {data.instrument.symbol}, from{' '}
        {new Date(data.candles[0].time * 1000).toLocaleString('en-IN', { timeZone: data.timezone })}{' '}
        through{' '}
        {new Date(data.candles.at(-1)!.time * 1000).toLocaleString('en-IN', {
          timeZone: data.timezone,
        })}
        . Latest close {data.candles.at(-1)!.close.toFixed(2)}.
      </p>
    </figure>
  );
}
