// @vitest-environment jsdom
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import {cleanup, render, screen} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import PaceAlert from './PaceAlert';

beforeAll(()=>{
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
});
afterEach(cleanup);

const poll={poll:2,threshold:7000,count_date:'2026-10-25',estimated_count_at:'2026-10-24T20:11:00Z',count_time_estimated:true,hours_left:41.5,gap:'2631',required_points_per_hour_now:40.2};
const threshold=(rates:Record<string,unknown>,extra:Record<string,unknown>={})=>({total:'4369',observed_at:'2026-10-22T02:50:00Z',snapshot_age_minutes:12,stale:false,rates,rate_basis:'12h',polls:[poll],...extra});
const show=(value:unknown)=>render(<MemoryRouter><PaceAlert threshold={value as any}/></MemoryRouter>);

describe('pace alert',()=>{
 it('warns when the 6-hour speed is below what the next poll needs and shows both speeds',()=>{
  show(threshold({'6h':{points_per_hour:29},'12h':{points_per_hour:58}}));
  expect(screen.getByText('最近 6 小时的得分速度低于 Poll 2 需要的速度')).toBeTruthy();
  expect(screen.getByText('需要 40.2 分/时 · 最近 6 小时 29 分/时 · 最近 12 小时 58 分/时')).toBeTruthy();
  expect(screen.getByText(/约 .* 计票（还剩 41.5 小时），距 7000 分还差 2631 分/)).toBeTruthy();
  expect(screen.getByRole('link',{name:'查看角色'}).getAttribute('href')).toBe('/ai/agents');
 });

 it('stays quiet when the pace is enough or cannot be measured',()=>{
  show(threshold({'6h':{points_per_hour:45},'12h':{points_per_hour:58}}));
  expect(screen.queryByRole('alert')).toBeNull();
  cleanup();
  show(threshold({'6h':null,'12h':{points_per_hour:58}}));
  expect(screen.queryByRole('alert')).toBeNull();
  cleanup();
  show(undefined);
  expect(screen.queryByRole('alert')).toBeNull();
 });

 it('says when the official total may be out of date',()=>{
  show(threshold({'6h':{points_per_hour:45}},{stale:true,snapshot_age_minutes:95}));
  expect(screen.getByText('总分可能过期')).toBeTruthy();
  expect(screen.getByText(/95 分钟前/)).toBeTruthy();
  expect(screen.getByRole('link',{name:'检查积分同步'}).getAttribute('href')).toBe('/operations');
 });
});
