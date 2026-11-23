// @vitest-environment jsdom
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen} from '@testing-library/react';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import {App} from 'antd';
import PollForecast from './PollForecast';

beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
});
let client:QueryClient;
afterEach(()=>{cleanup();client?.clear();vi.unstubAllGlobals();});

const snapshot={status:'ok',generated_at:'2026-10-23T02:05:00Z',model:'jev-latest',
 poll1:{shares:{yes:33.8,no:36.2,unsure:29.5}},
 baseline:{people:1519,probability:{yes:18.2,no:24.4,unsure:57.3}},
 latest:{window:['2026-10-22T02:00:00Z','2026-10-23T02:00:00Z'],people:1125,probability:{yes:21.3,no:24.0,unsure:54.7}},
 forecast:{shares:{yes:38.3,no:34.5,unsure:27.3},yes_of_decided:52.6,range_90:[47.6,57.5],outcomes:{yes_lead:0.58,close_call:0.36,no_lead:0.07}},
 changes:{people:767,net:{yes:25,no:-8,unsure:-17},transitions:[{from:'unsure',to:'yes',count:85}]},
 persuasion:{replied:{people:120,shift_points:{yes:6.7,no:-2.5,unsure:-4.2}},not_replied:{people:647,shift_points:{yes:2.6,no:-0.8,unsure:-1.8}}},
 history:[{window_end:'2026-10-23T02:00:00Z',yes_of_decided:52.6,range_90:[47.6,57.5],people:1125}],method:['已知 NPC = 比赛开始前创建的账号']};

function setup(payload:unknown){
 vi.stubGlobal('fetch',vi.fn(async()=>new Response(JSON.stringify(payload),{headers:{'Content-Type':'application/json'}})));
 client=new QueryClient({defaultOptions:{queries:{retry:false}}});
 render(<QueryClientProvider client={client}><App><PollForecast/></App></QueryClientProvider>);
}

describe('Poll forecast',()=>{
 it('shows the calibrated Poll 2 forecast, its chances and same-person changes',async()=>{
  setup(snapshot);
  expect(await screen.findByText('52.6%')).toBeTruthy();
  expect(screen.getByText('90% 区间 47.6% – 57.5%')).toBeTruthy();
  expect(screen.getByText('58%')).toBeTruthy();
  expect(screen.getByText('Yes +25')).toBeTruthy();
  expect(screen.getByText('No -8')).toBeTruthy();
  expect(screen.getByText('我们回复过 120 人：Yes +6.7 点 · No -2.5 点')).toBeTruthy();
  fireEvent.click(screen.getByText('立场变化明细'));
  expect(await screen.findByText('85')).toBeTruthy();
 },15000);
 it('explains why there is no forecast yet',async()=>{
  setup({status:'no_jev_connection'});
  expect(await screen.findByText('还没有可用的 Jev 连接，请在设置里添加 TypeSafe 连接')).toBeTruthy();
 });
});
