// @vitest-environment jsdom
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import {App} from 'antd';
import BlueCandidates from './BlueCandidates';
import {time} from '../common';

beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
 const style=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>style(element));
});
let client:QueryClient;
afterEach(()=>{cleanup();client?.clear();vi.unstubAllGlobals();});

const note='I think that this content is inauthentic (bot) activity because the account answered 30 of the 37 replies it received within 72 h.';
const planned={post_id:901,author_id:55,username:'spammy',created_at:'2026-10-22T01:00:00Z',kind:'reply',excerpt:'Vote now, vote now',cluster_id:'c1',cluster_size:10,p:0.93,ev:12.5,verified:false,plan_rank:1,slot_minute:0,
 evidence:[{rule:'quick_answers',log_odds:0.6,detail:'median reply time 56 s'},{rule:'behaviour_bot_rule',log_odds:2,detail:'answered 30 of 37 replies within 72 h'}],
 platform_url:'https://social.legitreal.com/posts/901',local_url:'/posts?post=901',suggested_reason:'Inauthentic (bot) / Spam',suggested_note:note};
const weak={...planned,post_id:902,plan_rank:undefined,slot_minute:undefined,p:0.6,suggested_note:null,platform_url:'https://social.legitreal.com/posts/902',local_url:'/posts?post=902'};
const payload=(extra:Record<string,unknown>={})=>({generated_at:'2026-10-22T03:00:00Z',day:'2026-10-22',threshold:0.85,submits_reports:false,stale:false,
 caveats:['p 值尚未经官方裁定校准','一次误报会让全队暂停举报 6 小时'],
 plan:{items:[planned],skipped:[{post_id:903,cluster_id:'c1',reason:'cluster_daily_cap'}],expected_value:12.5,stop_reason:'below_threshold'},
 candidates:[planned,weak],...extra});

function setup(data:unknown,initial='/blue') {
 const fetch=vi.fn(async(_input:RequestInfo|URL,_options?:RequestInit)=>new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}}));
 vi.stubGlobal('fetch',fetch);
 client=new QueryClient({defaultOptions:{queries:{retry:false}}});
 render(<QueryClientProvider client={client}><App><MemoryRouter initialEntries={[initial]}><BlueCandidates/></MemoryRouter></App></QueryClientProvider>);
 return fetch;
}

describe('Blue candidates',()=>{
 it('shows today\'s suggestions with evidence, the suggested note and links to report by hand',async()=>{
  setup(payload());
  expect(await screen.findByText('每条举报都要你在这里确认后，才会用 Simon 的账号提交')).toBeTruthy();
  expect(screen.getByText('一次误报会让全队暂停举报 6 小时')).toBeTruthy();
  const card=screen.getByText('Vote now, vote now',{selector:'.blue-excerpt'}).closest('article') as HTMLElement;
  // Account-level evidence sits on the account card around the post.
  const account=card.closest('.blue-account') as HTMLElement;
  expect(within(account).getByText('@spammy')).toBeTruthy();
  const evidence=Array.from(account.querySelectorAll(':scope > .blue-evidence li')).map(item=>item.textContent);
  expect(evidence).toEqual(['answered 30 of 37 replies within 72 h','median reply time 56 s']);
  expect(within(card).queryAllByRole('listitem')).toHaveLength(0);
  expect(within(card).getByText(note)).toBeTruthy();
  expect(within(card).getByText('举报类别：Inauthentic (bot) / Spam')).toBeTruthy();
  const open=within(card).getByRole('link',{name:/在平台打开 #901/});
  expect(open.getAttribute('href')).toBe('https://social.legitreal.com/posts/901');
  expect(open.getAttribute('target')).toBe('_blank');
  expect(open.getAttribute('rel')).toContain('noopener');
  expect(within(card).getByRole('link',{name:'本地帖子详情'}).getAttribute('href')).toBe('/posts?post=901');
 });

 it('copies the suggested note',async()=>{
  const writeText=vi.fn(async()=>undefined);
  Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText}});
  setup(payload());
  fireEvent.click(await screen.findByRole('button',{name:/复制说明/}));
  await waitFor(()=>expect(writeText).toHaveBeenCalledWith(note));
 });

 it('only planned posts get a report button, and only GETs run until someone confirms',async()=>{
  const fetch=setup(payload());
  await screen.findByText('今日建议');
  fireEvent.click(screen.getByText('可访问候选 · 2'));
  fireEvent.click(screen.getByText('未列入今日建议 · 1'));
  await screen.findByText('同一簇今天已满额');
  // The planned post has one; the candidate below the threshold has none.
  expect(screen.getAllByRole('button',{name:/用 Simon 的账号举报/})).toHaveLength(1);
  expect(fetch.mock.calls.every(([,options])=>!options?.method||options.method==='GET')).toBe(true);
 });

 it('files a report only after the confirmation dialog, with the exact note shown',async()=>{
  const posts:Array<[string,RequestInit|undefined]>=[];
  const fetch=vi.fn(async(input:RequestInfo|URL,options?:RequestInit)=>{
   const url=String(input);
   if(options?.method==='POST'){posts.push([url,options]);return new Response(JSON.stringify({post_id:901,status:'submitted'}),{headers:{'Content-Type':'application/json'}});}
   const body=url.startsWith('/api/bootstrap')?{csrf_token:'token-1'}:url.startsWith('/api/blue/reports')?{day:'2026-10-22',reports:[]}:payload();
   return new Response(JSON.stringify(body),{headers:{'Content-Type':'application/json'}});
  });
  vi.stubGlobal('fetch',fetch);
  client=new QueryClient({defaultOptions:{queries:{retry:false}}});
  render(<QueryClientProvider client={client}><App><MemoryRouter initialEntries={['/blue']}><BlueCandidates/></MemoryRouter></App></QueryClientProvider>);
  fireEvent.click(await screen.findByRole('button',{name:/用 Simon 的账号举报/}));
  const dialog=await screen.findByRole('dialog');
  expect(within(dialog).getAllByText('用 Simon 的账号举报帖子 #901？').length).toBeGreaterThan(0);
  expect(within(dialog).getByText(note)).toBeTruthy();
  // Ant Design spaces two-character labels ("取 消").
  fireEvent.click(within(dialog).getByRole('button',{name:/取\s*消/}));
  await waitFor(()=>expect(screen.queryByRole('dialog')).toBeNull());
  expect(posts).toEqual([]);
  fireEvent.click(screen.getByRole('button',{name:/用 Simon 的账号举报/}));
  fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button',{name:'确认提交'}));
  await waitFor(()=>expect(posts).toHaveLength(1));
  const [url,options]=posts[0];
  expect(url).toBe('/api/blue/report');
  expect(JSON.parse(String(options?.body))).toEqual({post_id:901,confirmed:true});
  expect((options?.headers as Record<string,string>)['X-CTN-Token']).toBe('token-1');
 });

 it('removes submitted posts from suggestions and keeps them in today\'s reports',async()=>{
  const fetch=vi.fn(async(input:RequestInfo|URL)=>{
   const url=String(input);
   const body=url.startsWith('/api/blue/reports')?{day:'2026-10-22',reports:[{post_id:901,username:'spammy',author_id:55,cluster_id:'c1',status:'submitted',at:'2026-10-22T09:00:00Z'}]}:payload();
   return new Response(JSON.stringify(body),{headers:{'Content-Type':'application/json'}});
  });
  vi.stubGlobal('fetch',fetch);
  client=new QueryClient({defaultOptions:{queries:{retry:false}}});
  render(<QueryClientProvider client={client}><App><MemoryRouter initialEntries={['/blue']}><BlueCandidates/></MemoryRouter></App></QueryClientProvider>);
  await screen.findByText('今天的举报记录 · 1');
  expect(screen.queryByText('Vote now, vote now',{selector:'.blue-excerpt'})).toBeNull();
  expect(screen.queryByRole('button',{name:/用 Simon 的账号举报/})).toBeNull();
  fireEvent.click(screen.getByText('今天的举报记录 · 1'));
  expect(await screen.findByText('已提交')).toBeTruthy();
 });

 it('loads cross-day report history only when expanded and preserves submission and failure details',async()=>{
  const records=[
   {post_id:899,username:'earlier-bot',author_id:54,cluster_id:'c2',status:'submitted',at:'2026-10-21T16:00:00Z'},
   {post_id:898,author_id:53,cluster_id:'c3',status:'failed',error:'Original platform error: post missing',at:'2026-10-20T16:00:00Z'},
  ];
  const fetch=vi.fn(async(input:RequestInfo|URL)=>{
   const url=String(input),body=url.startsWith('/api/blue/reports/history')?{reports:records,total:2,limit:20,offset:0}:url.startsWith('/api/blue/reports')?{reports:[]}:payload();
   return new Response(JSON.stringify(body),{headers:{'Content-Type':'application/json'}});
  });
  vi.stubGlobal('fetch',fetch);
  client=new QueryClient({defaultOptions:{queries:{retry:false}}});
  render(<QueryClientProvider client={client}><App><MemoryRouter initialEntries={['/blue']}><BlueCandidates/></MemoryRouter></App></QueryClientProvider>);
  await screen.findByText('今天的举报记录 · 0');
  expect(fetch.mock.calls.some(([input])=>String(input).includes('/history'))).toBe(false);
  fireEvent.click(screen.getByText('历史举报记录'));
  // The history loads after the panel opens; under a loaded test run that can take over a second.
  const link=await screen.findByRole('link',{name:'#899'},{timeout:3000}),table=link.closest('table') as HTMLElement;
  expect(link.getAttribute('href')).toBe('/posts?post=899');
  expect(within(table).getByText(time(records[0].at))).toBeTruthy();
  expect(within(table).getByText(time(records[1].at))).toBeTruthy();
  expect(within(table).getByText('@earlier-bot')).toBeTruthy();
  expect(within(table).getByText('#53')).toBeTruthy();
  expect(within(table).getByText('c3')).toBeTruthy();
  expect(within(table).getByText('已提交')).toBeTruthy();
  expect(within(table).getByText('失败：Original platform error: post missing')).toBeTruthy();
  expect(within(table).getAllByRole('row').slice(1).map(row=>within(row).getAllByRole('cell')[1].textContent)).toEqual(['#899','#898']);
  expect(screen.getByText(/已提交仅表示/)).toBeTruthy();
  expect(fetch.mock.calls.some(([input])=>String(input)==='/api/blue/reports/history?limit=20&offset=0')).toBe(true);
 });

 it('requests historical reports in server-side pages of twenty',async()=>{
  const records=Array.from({length:21},(_,index)=>({post_id:1000+index,author_id:55,cluster_id:'c1',status:'submitted',at:`2026-10-21T12:${String(59-index).padStart(2,'0')}:00Z`}));
  const fetch=vi.fn(async(input:RequestInfo|URL)=>{
   const url=String(input),offset=Number(new URL(url,'http://localhost').searchParams.get('offset')||0);
   const body=url.startsWith('/api/blue/reports/history')?{reports:records.slice(offset,offset+20),total:21,limit:20,offset}:url.startsWith('/api/blue/reports')?{reports:[]}:payload();
   return new Response(JSON.stringify(body),{headers:{'Content-Type':'application/json'}});
  });
  vi.stubGlobal('fetch',fetch);
  client=new QueryClient({defaultOptions:{queries:{retry:false}}});
  render(<QueryClientProvider client={client}><App><MemoryRouter initialEntries={['/blue']}><BlueCandidates/></MemoryRouter></App></QueryClientProvider>);
  fireEvent.click(await screen.findByText('历史举报记录'));
  await screen.findByText('#1000',{selector:'a'});
  expect(screen.queryByText('#1020',{selector:'a'})).toBeNull();
  fireEvent.click(screen.getByTitle('2'));
  expect(await screen.findByText('#1020',{selector:'a'})).toBeTruthy();
  expect(screen.queryByText('#1000',{selector:'a'})).toBeNull();
  expect(fetch.mock.calls.some(([input])=>String(input)==='/api/blue/reports/history?limit=20&offset=20')).toBe(true);
 });

 it('refreshes today, history and candidates immediately after a failed report',async()=>{
  let failed=false;
  const error='Platform could not find this post',record={post_id:901,author_id:55,username:'spammy',cluster_id:'c1',status:'failed',error,at:'2026-10-22T04:00:00Z'};
  const fetch=vi.fn(async(input:RequestInfo|URL,options?:RequestInit)=>{
   const url=String(input);
   if(options?.method==='POST'){failed=true;return new Response(JSON.stringify({error}),{status:502,headers:{'Content-Type':'application/json'}});}
   const refreshed={...planned,availability_checked_at:'2026-10-22T04:01:00Z',excerpt:'Freshly checked candidate'};
   const body=url==='/api/bootstrap'?{csrf_token:'token-1'}:url.startsWith('/api/blue/reports/history')?{reports:failed?[record]:[],total:failed?1:0,limit:20,offset:0}:url==='/api/blue/reports'?{reports:failed?[record]:[]}:failed?payload({plan:{items:[refreshed],skipped:[]},candidates:[refreshed]}):payload();
   return new Response(JSON.stringify(body),{headers:{'Content-Type':'application/json'}});
  });
  vi.stubGlobal('fetch',fetch);
  client=new QueryClient({defaultOptions:{queries:{retry:false}}});
  render(<QueryClientProvider client={client}><App><MemoryRouter initialEntries={['/blue']}><BlueCandidates/></MemoryRouter></App></QueryClientProvider>);
  fireEvent.click(await screen.findByText('历史举报记录'));
  await screen.findByText(/已提交仅表示/);
  fireEvent.click(screen.getByRole('button',{name:/用 Simon 的账号举报/}));
  fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button',{name:'确认提交'}));
  expect(await screen.findByText('今天的举报记录 · 1')).toBeTruthy();
  expect(await screen.findByText(`上次失败：${error}`)).toBeTruthy();
  expect(await screen.findByText(`失败：${error}`)).toBeTruthy();
  expect(await screen.findByText('Freshly checked candidate')).toBeTruthy();
  expect(screen.getByText(`平台核验于 ${time('2026-10-22T04:01:00Z')}`)).toBeTruthy();
  for(const path of ['/api/blue/reports','/api/blue/reports/history?limit=20&offset=0','/api/blue/candidates?limit=50'])expect(fetch.mock.calls.filter(([input])=>String(input)===path).length).toBeGreaterThanOrEqual(2);
 });

 it('does not offer to copy a note when the evidence is too weak',async()=>{
  setup(payload({plan:{items:[],skipped:[],expected_value:0,stop_reason:'below_threshold'},candidates:[weak]}));
  expect(await screen.findByText('今天没有达到阈值的候选。可以展开下面的可访问候选自行核对。')).toBeTruthy();
  fireEvent.click(screen.getByText('可访问候选 · 1'));
  expect(await screen.findByText('没有足够强的证据生成说明，建议先在本地帖子详情里核对。')).toBeTruthy();
  expect(screen.queryByRole('button',{name:/复制说明/})).toBeNull();
 });

 it('shows nothing unless the endpoint confirms it is read-only',async()=>{
  setup(payload({submits_reports:true}));
  expect(await screen.findByText('接口没有确认只读模式，页面不显示候选')).toBeTruthy();
  expect(screen.queryByText('Vote now, vote now')).toBeNull();
 });

 it('links each card to the account cluster holding its Blue cluster',async()=>{
  setup(payload());
  const card=(await screen.findByText('Vote now, vote now',{selector:'.blue-excerpt'})).closest('.blue-account') as HTMLElement;
  expect(within(card).getByRole('link',{name:'所属账号簇'}).getAttribute('href')).toBe('/analysis/clusters?blue_cluster=c1');
  expect(within(card).getByRole('link',{name:'账号详情'}).getAttribute('href')).toBe('/accounts?q=55');
 });

 it('shows only the candidates of the clusters named in the address',async()=>{
  const other={...planned,post_id:950,cluster_id:'a77',username:'loner',excerpt:'Lonely post',plan_rank:2,local_url:'/posts?post=950'};
  setup(payload({plan:{items:[planned,other],skipped:[{post_id:903,cluster_id:'c1',reason:'cluster_daily_cap'}],expected_value:20,stop_reason:'below_threshold'},candidates:[planned,weak,other]}),'/blue?clusters=a77,c9');
  const banner=await screen.findByText('只显示 Blue 簇 a77、c9 的候选');
  const alert=banner.closest('[role="alert"]') as HTMLElement;
  expect(within(alert).getByText('来自账号簇页：今日建议 1 条，全部候选 1 条。上面的数字仍是全天的计划。')).toBeTruthy();
  expect(within(alert).getByRole('link',{name:'显示全部候选'}).getAttribute('href')).toBe('/blue');
  expect(within(alert).getByRole('link',{name:'回到账号簇'}).getAttribute('href')).toBe('/analysis/clusters?blue_cluster=a77');
  expect(screen.getByText('Lonely post',{selector:'.blue-excerpt'})).toBeTruthy();
  expect(screen.queryByText('Vote now, vote now',{selector:'.blue-excerpt'})).toBeNull();
  expect(screen.getByText('可访问候选 · 1')).toBeTruthy();
  expect(screen.getByText('未列入今日建议 · 0')).toBeTruthy();
 });

 it('labels each card with its side and filters to No-side candidates',async()=>{
  const opponent={...planned,post_id:960,author_id:77,cluster_id:'a88',username:'nobot',excerpt:'Vote no, keep Kingstonia open',plan_rank:1,side:'no',side_counts:{no:6,yes:0},local_url:'/posts?post=960'};
  const ours={...planned,plan_rank:2,side:'yes',side_counts:{no:0,yes:9}};
  setup(payload({plan:{items:[opponent,ours],skipped:[{post_id:903,cluster_id:'c1',reason:'cluster_daily_cap'}],expected_value:25,stop_reason:'below_threshold'},
   candidates:[opponent,ours,{...weak,side:'unknown',side_counts:{no:0,yes:1}},{...planned,post_id:903,side:'yes'}]}));
  const card=(await screen.findByText('Vote now, vote now',{selector:'.blue-excerpt'})).closest('.blue-account') as HTMLElement;
  expect(within(card).getByText('Yes 方（我方）').getAttribute('title')).toBe('簇内帖子：No 0 条 · Yes 9 条');
  expect(screen.getAllByText('No 方').length).toBe(1);
  fireEvent.click(screen.getByText('只看 No 方 · 1'));
  await waitFor(()=>expect(screen.queryByText('Vote now, vote now',{selector:'.blue-excerpt'})).toBeNull());
  expect(screen.getByText('Vote no, keep Kingstonia open',{selector:'.blue-excerpt'})).toBeTruthy();
  expect(screen.getByText('可访问候选 · 1')).toBeTruthy();
  expect(screen.getByText('未列入今日建议 · 0')).toBeTruthy();
  fireEvent.click(screen.getByText('全部立场'));
  expect(await screen.findByText('可访问候选 · 4')).toBeTruthy();
 });

 it('reads the No-side filter from the address',async()=>{
  setup(payload({plan:{items:[{...planned,side:'yes'}],skipped:[],expected_value:12.5,stop_reason:'below_threshold'},candidates:[{...planned,side:'yes'}]}),'/blue?side=no');
  expect(await screen.findByText('今天没有达到阈值的 No 方候选。可以展开下面的可访问候选自行核对。')).toBeTruthy();
  expect(screen.getByText('可访问候选 · 0')).toBeTruthy();
 });

 it('marks a result that is being recomputed',async()=>{
  setup(payload({stale:true}));
  expect(await screen.findByText('更新中')).toBeTruthy();
 });

 it('keeps unreadable and unverified suggestions with precise access problems and no report action',async()=>{
  const problems=[
   {http_status:404,status:'unavailable',error:'post missing'},
   {http_status:410,status:'unavailable',error:'post gone'},
   {http_status:403,status:'unavailable',error:'permission denied'},
   {http_status:429,status:'unverified',error:'rate limit reached'},
   {http_status:null,status:'unverified',error:'read timed out'},
  ];
  const issues=problems.map((availability,index)=>({...planned,post_id:1100+index,excerpt:`Pending ${index}`,availability:{...availability,checked_at:'2026-10-22T05:00:00Z'}}));
  setup(payload({plan:{items:[],skipped:[]},candidates:[],availability_issues:issues}));
  expect(await screen.findByText('待核对建议 · 5')).toBeTruthy();
  expect(screen.getByText('HTTP 404 · 当前无法读取，可能隐藏或删除')).toBeTruthy();
  expect(screen.getByText('HTTP 410 · 当前无法读取，可能隐藏或删除')).toBeTruthy();
  expect(screen.getByText('HTTP 403 · 当前没有读取权限')).toBeTruthy();
  expect(screen.getByText('HTTP 429 · 读取额度受限，稍后再确认')).toBeTruthy();
  expect(screen.getByText('暂未确认可访问，可能是网络或平台暂时异常')).toBeTruthy();
  for(const problem of problems)expect(screen.getByText(problem.error)).toBeTruthy();
  expect(screen.getAllByText(`平台核验于 ${time('2026-10-22T05:00:00Z')}`)).toHaveLength(5);
  expect(within(screen.getByText('候选总数').parentElement as HTMLElement).getByText('5',{selector:'strong'})).toBeTruthy();
  expect(screen.getAllByRole('button',{name:'再次确认'})).toHaveLength(5);
  expect(screen.queryByRole('button',{name:/用 Simon 的账号举报/})).toBeNull();
  expect(screen.queryByText(/帖子已删除|确定已删除/)).toBeNull();
 });

 it('applies cluster and side filters to suggestions awaiting access confirmation',async()=>{
  const issue={...planned,side:'no',availability:{status:'unverified',error:'network unavailable'}};
  setup(payload({plan:{items:[],skipped:[]},candidates:[],availability_issues:[
   {...issue,post_id:1200,excerpt:'Matching pending'},
   {...issue,post_id:1201,side:'yes',excerpt:'Our side pending'},
   {...issue,post_id:1202,cluster_id:'c2',excerpt:'Other cluster pending'},
  ]}),'/blue?clusters=c1&side=no');
  expect(await screen.findByText('待核对建议 · 1')).toBeTruthy();
  expect(screen.getByText('Matching pending')).toBeTruthy();
  expect(screen.queryByText('Our side pending')).toBeNull();
  expect(screen.queryByText('Other cluster pending')).toBeNull();
  fireEvent.click(screen.getByText('全部立场'));
  expect(await screen.findByText('待核对建议 · 2')).toBeTruthy();
  expect(screen.getByText('Our side pending')).toBeTruthy();
  expect(screen.queryByText('Other cluster pending')).toBeNull();
 });

 it('only rechecks access and moves a recovered post back to reportable suggestions',async()=>{
  const issue={...planned,availability:{status:'unverified',http_status:429,error:'rate limit reached'}},initial=payload({plan:{items:[],skipped:[]},candidates:[],availability_issues:[issue]});
  const restored={...planned,availability_checked_at:'2026-10-22T06:00:00Z'},view=payload({plan:{items:[restored],skipped:[]},candidates:[restored],availability_issues:[]});
  const posts:Array<[string,RequestInit]>=[];
  const fetch=vi.fn(async(input:RequestInfo|URL,options?:RequestInit)=>{
   const url=String(input);
   if(options?.method==='POST'){posts.push([url,options]);return new Response(JSON.stringify({post_id:901,availability:{status:'available',http_status:200},view}),{headers:{'Content-Type':'application/json'}});}
   const body=url==='/api/bootstrap'?{csrf_token:'recheck-token'}:url==='/api/blue/reports'?{reports:[]}:initial;
   return new Response(JSON.stringify(body),{headers:{'Content-Type':'application/json'}});
  });
  vi.stubGlobal('fetch',fetch);client=new QueryClient({defaultOptions:{queries:{retry:false}}});
  render(<QueryClientProvider client={client}><App><MemoryRouter initialEntries={['/blue']}><BlueCandidates/></MemoryRouter></App></QueryClientProvider>);
  fireEvent.click(await screen.findByRole('button',{name:'再次确认'}));
  expect(await screen.findByRole('button',{name:/用 Simon 的账号举报/})).toBeTruthy();
  expect(screen.queryByText(/待核对建议/)).toBeNull();
  expect(screen.queryByRole('button',{name:'再次确认'})).toBeNull();
  expect(await screen.findByText('已确认可访问')).toBeTruthy();
  expect(posts.map(([url])=>url)).toEqual(['/api/blue/recheck']);
  expect(JSON.parse(String(posts[0][1].body))).toEqual({post_id:901,limit:50});
  expect((posts[0][1].headers as Record<string,string>)['X-CTN-Token']).toBe('recheck-token');
 });

 it('retains the problem card and specific error when a recheck request fails',async()=>{
  const issue={...planned,availability:{status:'unverified',http_status:null,error:'original timeout'}},initial=payload({plan:{items:[],skipped:[]},candidates:[],availability_issues:[issue]});
  const posts:string[]=[];let failed=false;
  const fetch=vi.fn(async(input:RequestInfo|URL,options?:RequestInit)=>{
   const url=String(input);
   if(options?.method==='POST'){failed=true;posts.push(url);return new Response(JSON.stringify({error:'Recheck service unavailable'}),{status:503,headers:{'Content-Type':'application/json'}});}
   if(failed&&url.startsWith('/api/blue/candidates'))return new Response(JSON.stringify({error:'Candidate refresh unavailable'}),{status:503,headers:{'Content-Type':'application/json'}});
   return new Response(JSON.stringify(url==='/api/bootstrap'?{csrf_token:'recheck-token'}:url==='/api/blue/reports'?{reports:[]}:initial),{headers:{'Content-Type':'application/json'}});
  });
  vi.stubGlobal('fetch',fetch);client=new QueryClient({defaultOptions:{queries:{retry:false}}});
  render(<QueryClientProvider client={client}><App><MemoryRouter initialEntries={['/blue']}><BlueCandidates/></MemoryRouter></App></QueryClientProvider>);
  fireEvent.click(await screen.findByRole('button',{name:'再次确认'}));
  expect(await screen.findByText('复核失败：Recheck service unavailable')).toBeTruthy();
  expect(await screen.findByText('候选刷新失败，保留上次结果')).toBeTruthy();
  expect(screen.getByText('待核对建议 · 1')).toBeTruthy();
  expect(screen.getByText('original timeout')).toBeTruthy();
  expect(screen.queryByRole('button',{name:/用 Simon 的账号举报/})).toBeNull();
  expect(posts).toEqual(['/api/blue/recheck']);
 });

 it.each(['unavailable','unverified'])('keeps a %s post pending after a completed recheck',async status=>{
  const issue={...planned,availability:{status:'unverified',http_status:null,error:'original timeout'}},initial=payload({plan:{items:[],skipped:[]},candidates:[],availability_issues:[issue]});
  const availability={status,http_status:status==='unavailable'?403:429,error:'latest access problem',checked_at:'2026-10-22T06:30:00Z'};
  const view=payload({plan:{items:[],skipped:[]},candidates:[],availability_issues:[{...planned,availability}]});
  const fetch=vi.fn(async(input:RequestInfo|URL,options?:RequestInit)=>new Response(JSON.stringify(options?.method==='POST'?{post_id:901,availability,view}:String(input)==='/api/bootstrap'?{csrf_token:'recheck-token'}:String(input)==='/api/blue/reports'?{reports:[]}:initial),{headers:{'Content-Type':'application/json'}}));
  vi.stubGlobal('fetch',fetch);client=new QueryClient({defaultOptions:{queries:{retry:false}}});
  render(<QueryClientProvider client={client}><App><MemoryRouter initialEntries={['/blue']}><BlueCandidates/></MemoryRouter></App></QueryClientProvider>);
  fireEvent.click(await screen.findByRole('button',{name:'再次确认'}));
  expect(await screen.findByText(status==='unavailable'?'仍无法访问':'仍未确认')).toBeTruthy();
  expect(screen.getByText('待核对建议 · 1')).toBeTruthy();
  expect(screen.getByText('latest access problem')).toBeTruthy();
  expect(screen.queryByRole('button',{name:/用 Simon 的账号举报/})).toBeNull();
 });

 it('refreshes away a pending card when the server rejects rechecking a concurrently submitted post',async()=>{
  let submitted=false;
  const initial=payload({plan:{items:[],skipped:[]},candidates:[],availability_issues:[{...planned,availability:{status:'unverified',error:'original timeout'}}]});
  const fetch=vi.fn(async(input:RequestInfo|URL,options?:RequestInit)=>{
   if(options?.method==='POST'){submitted=true;return new Response(JSON.stringify({error:'此帖已经举报'}),{status:400,headers:{'Content-Type':'application/json'}});}
   return new Response(JSON.stringify(String(input)==='/api/bootstrap'?{csrf_token:'recheck-token'}:String(input)==='/api/blue/reports'?{reports:[]}:submitted?payload({plan:{items:[],skipped:[]},candidates:[],availability_issues:[]}):initial),{headers:{'Content-Type':'application/json'}});
  });
  vi.stubGlobal('fetch',fetch);client=new QueryClient({defaultOptions:{queries:{retry:false}}});
  render(<QueryClientProvider client={client}><App><MemoryRouter initialEntries={['/blue']}><BlueCandidates/></MemoryRouter></App></QueryClientProvider>);
  fireEvent.click(await screen.findByRole('button',{name:'再次确认'}));
  await waitFor(()=>expect(screen.queryByText(/待核对建议/)).toBeNull());
  expect(screen.queryByText('Vote now, vote now',{selector:'.blue-excerpt'})).toBeNull();
  expect(fetch.mock.calls.filter(([input])=>String(input)==='/api/blue/candidates?limit=50').length).toBe(2);
 });

 it('keeps submitted posts out of the pending group even when candidate data is stale',async()=>{
  const fetch=vi.fn(async(input:RequestInfo|URL)=>new Response(JSON.stringify(String(input)==='/api/blue/reports'?{reports:[{post_id:901,status:'submitted',at:'2026-10-22T06:00:00Z'}]}:payload({plan:{items:[],skipped:[]},candidates:[],availability_issues:[{...planned,availability:{status:'unavailable',http_status:404,error:'post missing'}}]})),{headers:{'Content-Type':'application/json'}}));
  vi.stubGlobal('fetch',fetch);client=new QueryClient({defaultOptions:{queries:{retry:false}}});
  render(<QueryClientProvider client={client}><App><MemoryRouter initialEntries={['/blue']}><BlueCandidates/></MemoryRouter></App></QueryClientProvider>);
  await screen.findByText('今天的举报记录 · 1');
  expect(screen.queryByText(/待核对建议/)).toBeNull();
  expect(screen.queryByRole('button',{name:'再次确认'})).toBeNull();
  expect(screen.queryByText('Vote now, vote now',{selector:'.blue-excerpt'})).toBeNull();
 });
});

// The server's account rows (blue_view._accounts): evidence, hidden posts, and whether an account report is ready.
const readyAccount={account_id:55,username:'spammy',p:0.93,cluster_id:'c1',cluster_size:10,side:'no',side_counts:{no:4,yes:0},planned:2,copy_evidence:null,
 evidence:[{rule:'behaviour_bot_rule',log_odds:2,detail:'answered 30 of 37 replies within 72 h'}],
 account_report:{eligible:true,missing:[],hidden:2,suspect:1,actions:180,suggested_reason:'Inauthentic (bot) / Spam',suggested_note:'I think that this account is inauthentic (bot) activity because the account answered 30 of the 37 replies it received within 72 h.'}};
const waitingAccount={...readyAccount,account_id:66,username:'quiet',cluster_id:'c2',planned:1,account_report:{eligible:false,missing:['hidden','actions'],hidden:0,suspect:0,actions:40}};
const second={...planned,post_id:904,excerpt:'Second post of spammy',plan_rank:2};
const quietPost={...planned,post_id:905,author_id:66,username:'quiet',cluster_id:'c2',excerpt:'Quiet post',plan_rank:3};

function mount(data:unknown,reports:Row[]=[]) {
 const posts:Array<[string,RequestInit]>=[];
 const fetch=vi.fn(async(input:RequestInfo|URL,options?:RequestInit)=>{
  const url=String(input);
  if(options?.method==='POST'){posts.push([url,options]);return new Response(JSON.stringify({target:'account',author_id:55,status:'submitted'}),{headers:{'Content-Type':'application/json'}});}
  const body=url==='/api/bootstrap'?{csrf_token:'token-2'}:url.startsWith('/api/blue/reports/history')?{reports,total:reports.length,limit:20,offset:0}:url.startsWith('/api/blue/reports')?{reports}:data;
  return new Response(JSON.stringify(body),{headers:{'Content-Type':'application/json'}});
 });
 vi.stubGlobal('fetch',fetch);
 client=new QueryClient({defaultOptions:{queries:{retry:false}}});
 render(<QueryClientProvider client={client}><App><MemoryRouter initialEntries={['/blue']}><BlueCandidates/></MemoryRouter></App></QueryClientProvider>);
 return posts;
}
type Row=Record<string,unknown>;

describe('Blue by account',()=>{
 const byAccount=()=>payload({plan:{items:[quietPost,planned,second],skipped:[],expected_value:30,stop_reason:'exhausted'},candidates:[quietPost,planned,second,weak],
  accounts:[readyAccount,waitingAccount]});

 it('groups an account\'s posts into one card, in the server\'s account order',async()=>{
  mount(byAccount());
  const cards=await screen.findAllByRole('region',{name:/^账号 @/});
  expect(cards.map(card=>card.getAttribute('aria-label'))).toEqual(['账号 @spammy','账号 @quiet']);
  const spammy=cards[0];
  expect(within(spammy).getByText('Vote now, vote now')).toBeTruthy();
  expect(within(spammy).getByText('Second post of spammy')).toBeTruthy();
  expect(within(spammy).getAllByRole('button',{name:'用 Simon 的账号举报'})).toHaveLength(2);
  expect(within(spammy).getByText('已确认隐藏 2 条 · 待复核 1 条')).toBeTruthy();
  expect(within(spammy).getByText('采集到的动作 180 次')).toBeTruthy();
  const metric=screen.getByText('可举报账号').parentElement as HTMLElement;
  expect(within(metric).getByText('1',{selector:'strong'})).toBeTruthy();
  // Counts stay apart by side: Yes is our side (D49).
  expect(within(metric).getByText(/No 方 1 · 立场不明\/混合 0 · Yes 方（我方）0/)).toBeTruthy();
  expect(screen.getByText('2 个账号（No 方 2 · 立场不明/混合 0 · Yes 方（我方）0）；每个账号先报帖子，满足条件后再举报账号。')).toBeTruthy();
 });

 it('says what an account still lacks and offers no account report until it is ready',async()=>{
  mount(byAccount());
  const quiet=await screen.findByRole('region',{name:'账号 @quiet'});
  expect(within(quiet).getByText('账号举报：还不能举报 — 还没有确认隐藏的帖子；采集到的动作不到 100 次')).toBeTruthy();
  expect(within(quiet).queryByRole('button',{name:/举报这个账号/})).toBeNull();
  expect(within(quiet).getByRole('button',{name:'用 Simon 的账号举报'})).toBeTruthy();
 });

 it('names the three-hide suspension line as the reason an account gets no account report',async()=>{
  const suspended={...waitingAccount,account_report:{eligible:false,missing:['suspension_line','actions'],hidden:3,suspect:0,actions:97}};
  mount({...byAccount(),accounts:[readyAccount,suspended]});
  const quiet=await screen.findByRole('region',{name:'账号 @quiet'});
  expect(within(quiet).getByText('账号举报：还不能举报 — 已到 3 条封号线，可能已被封；采集到的动作不到 100 次')).toBeTruthy();
  expect(within(quiet).queryByRole('button',{name:/举报这个账号/})).toBeNull();
 });

 it('files an account report only after its confirmation dialog',async()=>{
  const posts=mount(byAccount());
  const spammy=await screen.findByRole('region',{name:'账号 @spammy'});
  expect(within(spammy).getByText(readyAccount.account_report.suggested_note)).toBeTruthy();
  fireEvent.click(within(spammy).getByRole('button',{name:'用 Simon 的账号举报这个账号'}));
  const dialog=await screen.findByRole('dialog');
  expect(within(dialog).getAllByText('用 Simon 的账号举报账号 @spammy（#55）？').length).toBeGreaterThan(0);
  expect(within(dialog).getByText('已确认隐藏 2 条 · 采集到的动作 180 次 · 可信度 93% · 簇 c1')).toBeTruthy();
  fireEvent.click(within(dialog).getByRole('button',{name:/取\s*消/}));
  await waitFor(()=>expect(screen.queryByRole('dialog')).toBeNull());
  expect(posts).toEqual([]);
  fireEvent.click(within(spammy).getByRole('button',{name:'用 Simon 的账号举报这个账号'}));
  fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button',{name:'确认提交'}));
  await waitFor(()=>expect(posts).toHaveLength(1));
  expect(posts[0][0]).toBe('/api/blue/report-account');
  expect(JSON.parse(String(posts[0][1].body))).toEqual({account_id:55,confirmed:true});
  expect((posts[0][1].headers as Record<string,string>)['X-CTN-Token']).toBe('token-2');
 });

 it('marks a reported account and lists account reports in the records',async()=>{
  mount(byAccount(),[{target:'account',post_id:null,author_id:55,username:'spammy',cluster_id:'c1',status:'submitted',at:'2026-10-22T08:00:00Z'}]);
  const spammy=await screen.findByRole('region',{name:'账号 @spammy'});
  expect(await within(spammy).findByText(`已举报账号 · ${time('2026-10-22T08:00:00Z')}`)).toBeTruthy();
  expect(within(spammy).queryByRole('button',{name:'用 Simon 的账号举报这个账号'})).toBeNull();
  // An account report does not mark the account's posts as reported.
  expect(within(spammy).getAllByRole('button',{name:'用 Simon 的账号举报'})).toHaveLength(2);
  fireEvent.click(screen.getByText('今天的举报记录 · 1'));
  const row=(await screen.findByText('账号',{selector:'.ant-tag'})).closest('tr') as HTMLElement;
  expect(within(row).getByText('@spammy')).toBeTruthy();
  expect(within(row).getByText('—')).toBeTruthy();
 });

 it('keeps the account report button for Yes-side accounts and counts them apart',async()=>{
  const ours={...readyAccount,account_id:88,username:'yesbot',cluster_id:'c8',side:'yes',side_counts:{no:0,yes:6}};
  const oursPost={...planned,post_id:907,author_id:88,username:'yesbot',cluster_id:'c8',side:'yes',excerpt:'Yes-side post',plan_rank:4};
  mount({...byAccount(),plan:{items:[quietPost,planned,second,oursPost],skipped:[],expected_value:40,stop_reason:'exhausted'},
   candidates:[quietPost,planned,second,oursPost],accounts:[readyAccount,waitingAccount,ours]});
  const yes=await screen.findByRole('region',{name:'账号 @yesbot'});
  expect(within(yes).getByText('Yes 方（我方）')).toBeTruthy();
  expect(within(yes).getByRole('button',{name:'用 Simon 的账号举报这个账号'})).toBeTruthy();
  const metric=screen.getByText('可举报账号').parentElement as HTMLElement;
  expect(within(metric).getByText('2',{selector:'strong'})).toBeTruthy();
  expect(within(metric).getByText(/No 方 1 · 立场不明\/混合 0 · Yes 方（我方）1/)).toBeTruthy();
 });

 it('lists an account whose account report alone is ready',async()=>{
  const alone={...readyAccount,account_id:77,username:'idle',cluster_id:'c7',planned:0};
  const idlePost={...weak,post_id:906,author_id:77,username:'idle',cluster_id:'c7',excerpt:'Idle candidate'};
  mount(payload({plan:{items:[],skipped:[],expected_value:0,stop_reason:'below_threshold'},candidates:[idlePost],accounts:[alone]}));
  const idle=await screen.findByRole('region',{name:'账号 @idle'});
  expect(within(idle).getByText('Idle candidate')).toBeTruthy();
  expect(within(idle).getByRole('button',{name:'用 Simon 的账号举报这个账号'})).toBeTruthy();
  expect(within(idle).queryByRole('button',{name:'用 Simon 的账号举报'})).toBeNull();
 });

 it('shows shared wording and the style group apart when the server sends them',async()=>{
  const style={style_group:'S6301',group_size:14,group_listed:12,group_hidden_accounts:5,peers:[61,62],linked_to_ours:0,possible_teammate:false,
   examples:[{post_id:5001,account_id:61,username:'peer_one',excerpt:'Same skeleton, other words'}]};
  const copy={template_share:0.4,copied_facts:2,phrases:[{phrase:'is the worry people keep raising',accounts:12,npc_free:true},{phrase:'people ask me every day',accounts:30,npc_free:false}],peers:[56,57,58],style};
  mount({...byAccount(),accounts:[{...readyAccount,copy_evidence:copy},{...waitingAccount,copy_evidence:{style}}]});
  const spammy=await screen.findByRole('region',{name:'账号 @spammy'});
  expect(within(spammy).getByText('文案：模板帖占比 40% · 共用短语账号 3 个 · 照抄事实 2 条')).toBeTruthy();
  expect(within(spammy).getByText('“is the worry people keep raising” · 12 个账号')).toBeTruthy();
  expect(within(spammy).getByText('“people ask me every day” · 30 个账号 · NPC 也用')).toBeTruthy();
  expect(within(spammy).getByText('文风：同组 14 人（其中被隐藏 5 个） · 组 S6301')).toBeTruthy();
  expect(within(spammy).getByRole('link',{name:'@peer_one'}).getAttribute('href')).toBe('/posts?post=5001');
  // Style alone: no wording line.
  const quiet=await screen.findByRole('region',{name:'账号 @quiet'});
  expect(within(quiet).queryByText(/^文案：/)).toBeNull();
  expect(within(quiet).getByText('文风：同组 14 人（其中被隐藏 5 个） · 组 S6301')).toBeTruthy();
 });

 it('warns before reporting an account written like ours',async()=>{
  mount({...byAccount(),accounts:[{...readyAccount,linked_to_ours:2},waitingAccount]});
  const spammy=await screen.findByRole('region',{name:'账号 @spammy'});
  expect(within(spammy).getByText('与我方 2 个账号文风相近，举报前请核对')).toBeTruthy();
  expect(within(spammy).getByRole('button',{name:'用 Simon 的账号举报这个账号'})).toBeTruthy();
 });

 it('lists probable teammates apart with the reason and no report button',async()=>{
  const mate={...readyAccount,account_id:3925,username:'sidequest01',cluster_id:'c9',possible_teammate:true,teammate_note:'与我方文风同组，可能是未登记的队友',
   account_report:{eligible:false,missing:['possible_teammate'],hidden:1,suspect:0,actions:200},posts:[{...planned,post_id:908,author_id:3925,username:'sidequest01',excerpt:'Mate post',plan_rank:undefined}]};
  mount({...byAccount(),excluded_teammates:[mate]});
  await screen.findByRole('region',{name:'账号 @spammy'});
  expect(screen.queryByRole('region',{name:'账号 @sidequest01'})).toBeNull();
  fireEvent.click(screen.getByText('可能是未登记的队友，已剔除 · 1'));
  const card=await screen.findByRole('region',{name:'账号 @sidequest01'});
  expect(within(card).getAllByText('与我方文风同组，可能是未登记的队友').length).toBeGreaterThan(0);
  expect(within(card).getByText('Mate post')).toBeTruthy();
  expect(within(card).queryByRole('button',{name:/用 Simon 的账号举报/})).toBeNull();
 });
});
