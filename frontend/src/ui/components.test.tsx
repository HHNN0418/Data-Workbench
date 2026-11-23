// @vitest-environment jsdom
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {MemoryRouter, useLocation} from 'react-router-dom';
import {IdentityTags, RelativeTime, StanceTag, StatusTag} from './status';
import {Advanced, MoreMenu, NextStep, PageHeader, errorHint} from './layout';

beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
 const style=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>style(element));
});
afterEach(cleanup);
function Location() {const location=useLocation();return <output data-testid="location">{location.pathname}</output>;}

describe('status display',()=>{
 it('maps the five tones to antd status colours',()=>{
  const {container}=render(<>{(['ok','warn','danger','info','neutral'] as const).map(tone=><StatusTag key={tone} tone={tone}>{tone}</StatusTag>)}</>);
  const tags=Array.from(container.querySelectorAll('[data-tone]'));
  expect(tags.map(tag=>tag.getAttribute('data-tone'))).toEqual(['ok','warn','danger','info','neutral']);
  expect(tags[0].className).toContain('ant-tag-success');
  expect(tags[2].className).toContain('ant-tag-error');
 });

 it('labels stance, marks our side from the team setting and tells unlabelled apart from unrelated',()=>{
  render(<><StanceTag stance="yes" teamSide="Vote Yes"/><StanceTag stance="no" teamSide="Vote Yes"/><StanceTag stance={null}/><StanceTag stance="unrelated"/></>);
  expect(screen.getByText('支持 Yes').closest('[data-stance]')?.textContent).toBe('支持 Yes我方立场');
  expect(screen.getByText('反对 No').closest('[data-stance]')?.textContent).toBe('反对 No');
  expect(screen.getByText('未标注')).toBeTruthy();
  expect(screen.getByText('无关')).toBeTruthy();
 });

 it('marks our side for a No team too',()=>{
  render(<StanceTag stance="no" teamSide="Vote No"/>);
  expect(screen.getByText('我方立场')).toBeTruthy();
 });

 it('shows suspected bots only from behaviour and duplicate text as a lighter mark',()=>{
  render(<><IdentityTags isOwn={1}/><IdentityTags isBot={true} botReasons={['behavior','duplicate_text']}/><IdentityTags isBot={false} botReasons={['duplicate_text']}/></>);
  expect(screen.getByText('本队')).toBeTruthy();
  expect(screen.getAllByText('疑似机器人')).toHaveLength(1);
  expect(screen.getAllByText('疑似重复文本')).toHaveLength(1);
  const {container}=render(<IdentityTags isOwn={0} isBot={false} botReasons={[]}/>);
  expect(container.textContent).toBe('');
 });

 it('writes recent times relative to now with the full time on hover',()=>{
  const now=Date.parse('2026-10-22T03:00:00Z');
  render(<><RelativeTime value="2026-10-22T02:57:00Z" now={now}/><RelativeTime value="2026-10-21T23:00:00Z" now={now}/><RelativeTime value={now/1000+600} now={now}/><RelativeTime value={null} now={now}/></>);
  expect(screen.getByText('3 分钟前').getAttribute('title')).toBe('2026/10/22 13:57');
  expect(screen.getByText('4 小时前')).toBeTruthy();
  expect(screen.getByText('10 分钟后')).toBeTruthy();
  expect(screen.getByText('—')).toBeTruthy();
 });
});

describe('layout helpers',()=>{
 it('puts page actions on the right and renders nothing when empty',()=>{
  const {container}=render(<PageHeader/>);
  expect(container.textContent).toBe('');
  render(<PageHeader note="最近观测 13:00" actions={<button>导出</button>}/>);
  expect(screen.getByText('最近观测 13:00')).toBeTruthy();
  expect(screen.getByRole('button',{name:'导出'})).toBeTruthy();
 });

 it('runs menu items: route links navigate, downloads stay links, actions are called',async()=>{
  const action=vi.fn();
  render(<MemoryRouter><MoreMenu items={[{key:'csv',label:'导出 CSV',href:'/api/export?table=posts'},{key:'raw',label:'原始记录',to:'/raw'},{key:'fill',label:'补录',onClick:action}]}/><Location/></MemoryRouter>);
  fireEvent.click(screen.getByRole('button',{name:/更多/}));
  expect((await screen.findByText('导出 CSV')).closest('a')?.getAttribute('href')).toBe('/api/export?table=posts');
  fireEvent.click(screen.getByText('补录'));
  expect(action).toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button',{name:/更多/}));
  fireEvent.click(await screen.findByText('原始记录'));
  await waitFor(()=>expect(screen.getByTestId('location').textContent).toBe('/raw'));
 });

 it('keeps advanced content closed until asked',async()=>{
  render(<Advanced><p>相关性明细</p></Advanced>);
  expect(screen.queryByText('相关性明细')).toBeNull();
  fireEvent.click(screen.getByText('高级 / 导出'));
  expect(await screen.findByText('相关性明细')).toBeTruthy();
 });

 it('gives an empty state a next step',()=>{
  render(<MemoryRouter><NextStep title="还没有角色" description="先新建一个角色" action={{label:'新建角色',to:'/ai/agents'}}/></MemoryRouter>);
  expect(screen.getByText('还没有角色')).toBeTruthy();
  expect(screen.getByRole('link',{name:'新建角色'}).getAttribute('href')).toBe('/ai/agents');
 });

 it.each([
  ['请求失败 (502)','后端暂时没有响应','/operations'],
  ['Failed to fetch','后端暂时没有响应','/operations'],
  ['请求失败 (503)','系统维护中',undefined],
  ['请求失败 (404)','接口还不可用',undefined],
  ['作者 ID 无效','读取失败',undefined]
 ])('explains %s',(message,title,to)=>{
  const hint=errorHint(new Error(message));
  expect(hint.title).toBe(title);
  expect(hint.action?.to).toBe(to);
 });
});
