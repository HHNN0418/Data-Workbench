// @vitest-environment jsdom
import {afterEach,describe,expect,it,vi} from 'vitest';
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {Details,fmt,time} from './common';

afterEach(cleanup);

describe('shared display formatting',()=>{
 it('preserves number formatting and missing values',()=>{
  expect(fmt(12345.67891)).toBe('12,345.6789');
  expect(fmt(0)).toBe('0');
  expect(fmt(false)).toBe('否');
  expect(fmt(true)).toBe('是');
  for(const value of [null,undefined,'',NaN,Infinity])expect(fmt(value)).toBe('—');
  expect(fmt({count:1})).toBe('{"count":1}');
 });
 it('keeps Sydney daylight saving and invalid timestamp handling',()=>{
  expect(time('2026-01-01T00:00:00Z')).toBe('2026/01/01 11:00');
  expect(time('2026-07-01T00:00:00Z')).toBe('2026/07/01 10:00');
  expect(time('not-a-timestamp')).toBe('not-a-timestamp');
  expect(time(null)).toBe('—');
 });
});

describe('collapsed details',()=>{
 it('serializes only expanded data and refreshes it when the data changes',async()=>{
  const first=vi.fn(()=>({text:'first report'})),next=vi.fn(()=>({text:'updated report'}));
  const view=render(<Details data={{toJSON:first}}/>);
  expect(first).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText('完整字段'));
  await screen.findByText(/first report/);
  expect(first).toHaveBeenCalledTimes(1);
  view.rerender(<Details data={{toJSON:next}}/>);
  expect(screen.getByText(/updated report/)).toBeTruthy();
  expect(next).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByText('完整字段'));
  await waitFor(()=>expect(screen.queryByText(/updated report/)).toBeNull());
  const closed=vi.fn(()=>({text:'hidden update'}));
  view.rerender(<Details data={{toJSON:closed}}/>);
  expect(closed).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText('完整字段'));
  await screen.findByText(/hidden update/);
  expect(closed).toHaveBeenCalledTimes(1);
 });
});
