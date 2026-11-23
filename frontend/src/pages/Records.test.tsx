// @vitest-environment jsdom
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import {MemoryRouter, useLocation} from 'react-router-dom';
import {News} from './Records';

vi.mock('antd',async importOriginal=>{
 const original=await importOriginal<typeof import('antd')>();
 // Keep real router, queries and page behavior. Host layout and CSS are not
 // under test; expensive jsdom style computation must not obscure the news
 // source, timestamp, navigation and active-view assertions below.
 return {...original,
  Tabs:({items,activeKey,onChange}:any)=><div role="tablist">{items.map((item:any)=><button key={item.key} type="button" role="tab" aria-selected={activeKey===item.key} onClick={()=>onChange(item.key)}>{item.label}</button>)}</div>,
  Pagination:()=>null,
  Descriptions:({items}:any)=><dl>{items.map((item:any)=><div className="ant-descriptions-item" key={item.key}><dt>{item.label}</dt><dd>{item.children}</dd></div>)}</dl>,
  Drawer:({open,title,children}:any)=>open?<aside role="dialog" aria-label={title}>{children}</aside>:null,
  Table:({dataSource,columns}:any)=><table><thead><tr>{columns.map((column:any,index:number)=><th key={column.key??column.dataIndex??index}>{column.title}</th>)}</tr></thead><tbody>{dataSource.map((row:any,index:number)=><tr key={row.post_id??row.tag_id??row.article_key??index}>{columns.map((column:any,columnIndex:number)=><td key={column.key??column.dataIndex??columnIndex}>{column.render?column.render(row[column.dataIndex],row,index):row[column.dataIndex]}</td>)}</tr>)}</tbody></table>};
});
beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 const originalStyle=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>originalStyle(element));
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn().mockImplementation(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
});
const clients:QueryClient[]=[];
afterEach(()=>{cleanup();clients.forEach(client=>client.clear());clients.length=0;vi.unstubAllGlobals();});
const observed='2026-10-20T01:00:00Z';
const newsAccount={username:'DailyKingstonia',account_id:1,posts_collected:16,observed_at:observed,cursor:'next-page',completed_at:null};
const metadata={popular_tags:{count:2,observed_at:observed},all_tags:{observed_at:observed,cursor:'page-2',completed_at:null},news_accounts:[newsAccount]};
function Location(){return <output data-testid="location">{useLocation().search}</output>;}
function setup(initial='/news',articleOverrides:Record<string,unknown>={}){
 const fetch=vi.fn((input:RequestInfo|URL)=>{
  const url=new URL(String(input),'http://localhost');
  const items=url.pathname==='/api/news'?[{article_key:'article-1',title:'新闻标题',site:'Daily Kingstonia',body:'新闻文章的完整正文',published_at:'2026-10-20T12:30:00Z',first_seen:observed,last_seen:'2026-10-20T01:15:00Z',...articleOverrides}]:url.searchParams.get('view')==='news_accounts'?[{post_id:21,body:'新闻账号帖子的完整正文',username:'DailyKingstonia',display_name:'Daily Kingstonia',created_at:observed}]:url.searchParams.get('popular')==='false'?[{tag_id:2,name:'healthchoices',first_seen:observed,last_seen:observed}]:[{tag_id:1,name:'protectkingstonia',post_count:743,popular_rank:1,observed_at:observed}];
  return Promise.resolve(new Response(JSON.stringify({...metadata,items,total:items.length,offset:Number(url.searchParams.get('offset')||0),limit:Number(url.searchParams.get('limit')||25)}),{status:200,headers:{'Content-Type':'application/json'}}));
 });
 vi.stubGlobal('fetch',fetch);
 const client=new QueryClient({defaultOptions:{queries:{retry:false}}});clients.push(client);
 render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[initial]}><News/><Location/></MemoryRouter></QueryClientProvider>);
 return fetch;
}

describe('news and social context',()=>{
 it('keeps news articles as the default and loads only the active view',async()=>{
  const fetch=setup();
  fireEvent.click(await screen.findByRole('button',{name:'新闻标题'}));
  expect(await screen.findByText('新闻文章的完整正文')).toBeTruthy();
  expect(screen.getByRole('tab',{name:'新闻文章'}).getAttribute('aria-selected')).toBe('true');
  expect(fetch.mock.calls.every(([path])=>String(path).startsWith('/api/news?'))).toBe(true);
  expect(screen.getByRole('link',{name:'导出新闻 CSV'}).getAttribute('href')).toBe('/api/export?table=news');
 });
 it('distinguishes fictional publication from actual collection and displays both in Sydney time',async()=>{
  setup();
  const title=await screen.findByRole('button',{name:'新闻标题'});
  const table=screen.getByRole('table');
  expect(within(table).getByRole('columnheader',{name:'剧情发布时间（悉尼）'})).toBeTruthy();
  expect(within(table).getByRole('columnheader',{name:'首次采集（悉尼）'})).toBeTruthy();
  expect(within(table).queryByRole('columnheader',{name:'发布时间'})).toBeNull();
  expect(within(table).getByRole('cell',{name:'2026/10/20 23:30'})).toBeTruthy();
  expect(within(table).getByRole('cell',{name:'2026/10/20 12:00'})).toBeTruthy();
  expect(screen.getByText('剧情发布时间来自源站，可能领先现实时间；首次采集为本系统发现时间，均按悉尼时区显示。')).toBeTruthy();
  fireEvent.click(title);
  const dialog=screen.getByRole('dialog',{name:'新闻标题'});
  for(const [label,value] of [['来源','Daily Kingstonia'],['剧情发布时间（悉尼）','2026/10/20 23:30'],['首次采集（悉尼）','2026/10/20 12:00'],['最近采集（悉尼）','2026/10/20 12:15']]){
   const item=within(dialog).getByText(label).closest('.ant-descriptions-item');
   expect(item).toBeTruthy();
   expect(within(item as HTMLElement).getByText(value)).toBeTruthy();
  }
 });
 it('keeps an unavailable first collection time empty instead of substituting the fictional publication time',async()=>{
  setup('/news',{first_seen:undefined});
  const title=await screen.findByRole('button',{name:'新闻标题'});
  const table=screen.getByRole('table');
  expect(within(table).getByRole('cell',{name:'2026/10/20 23:30'})).toBeTruthy();
  expect(within(table).getByRole('cell',{name:'—'})).toBeTruthy();
  expect(within(table).queryByRole('cell',{name:'2026/10/20 12:00'})).toBeNull();
  fireEvent.click(title);
  const dialog=screen.getByRole('dialog',{name:'新闻标题'});
  const firstSeen=within(dialog).getByText('首次采集（悉尼）').closest('.ant-descriptions-item');
  expect(firstSeen).toBeTruthy();
  expect(within(firstSeen as HTMLElement).getByText('—')).toBeTruthy();
  expect(within(firstSeen as HTMLElement).queryByText('2026/10/20 23:30')).toBeNull();
 });
 it('shows the observed popular ranking, distinguishes collected tags, and links to local post search',async()=>{
  const fetch=setup('/news?news_view=tags&offset=25');
  expect((await screen.findByRole('link',{name:'#protectkingstonia'})).getAttribute('href')).toBe('/posts?feed=all&q=%23protectkingstonia');
  expect(screen.getByText('743')).toBeTruthy();
  expect(screen.getByText('最新热门榜：2 个标签')).toBeTruthy();
  expect(screen.getByText(/不代表平台全部标签/)).toBeTruthy();
  expect(screen.getByText('目录分页补采中')).toBeTruthy();
  expect(fetch.mock.calls.some(([path])=>String(path).includes('/api/news?'))).toBe(false);
  fireEvent.click(screen.getByRole('tab',{name:'全部已采标签'}));
  expect(await screen.findByRole('link',{name:'#healthchoices'})).toBeTruthy();
  expect(screen.queryByRole('link',{name:'#protectkingstonia'})).toBeNull();
  const input=screen.getByLabelText('搜索标签');
  fireEvent.change(input,{target:{value:'#health'}});fireEvent.keyDown(input,{key:'Enter',code:'Enter',keyCode:13});
  await waitFor(()=>{
   const last=new URL(String(fetch.mock.calls.at(-1)?.[0]),'http://localhost');
   expect(last.searchParams.get('view')).toBe('tags');expect(last.searchParams.get('popular')).toBe('false');
   expect(last.searchParams.get('q')).toBe('#health');expect(last.searchParams.get('offset')).toBe('0');
  });
 });
 it('opens the collected news account body and links to its post details',async()=>{
  const fetch=setup('/news?news_view=news_accounts');
  fireEvent.click(await screen.findByRole('button',{name:'查看正文 #21'}));
  expect(screen.getByRole('dialog',{name:'新闻账号帖子 #21'})).toBeTruthy();
  expect(screen.getAllByText('新闻账号帖子的完整正文')).toHaveLength(2);
  expect(screen.getByText('已采集 16 条帖子')).toBeTruthy();
  expect(screen.getByText('历史分页补采中')).toBeTruthy();
  expect(screen.getByRole('link',{name:'查看帖子详情 #21'}).getAttribute('href')).toBe('/posts?feed=all&post=21');
  expect(screen.getByRole('link',{name:'查看该账号帖子'}).getAttribute('href')).toBe('/posts?feed=all&author_id=1');
  expect(fetch.mock.calls.every(([path])=>String(path).startsWith('/api/social-context?view=news_accounts'))).toBe(true);
 });
 it('follows tag and post links inside the app instead of reloading the page',async()=>{
  setup('/news?news_view=tags');
  fireEvent.click(await screen.findByRole('link',{name:'#protectkingstonia'}));
  await waitFor(()=>expect(screen.getByTestId('location').textContent).toBe('?feed=all&q=%23protectkingstonia'));
  cleanup();
  setup('/news?news_view=news_accounts');
  fireEvent.click(await screen.findByRole('button',{name:'查看正文 #21'}));
  fireEvent.click(screen.getByRole('link',{name:'查看帖子详情 #21'}));
  await waitFor(()=>expect(screen.getByTestId('location').textContent).toBe('?feed=all&post=21'));
 });
});
