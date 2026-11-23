// @vitest-environment jsdom
import {afterEach, describe, expect, it, vi} from 'vitest';
import {act, cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {focusManager, onlineManager, QueryClient, QueryClientProvider} from '@tanstack/react-query';
import type {ReactNode} from 'react';
import {useAction, useAnalysisRefresh, useApi, useLocalDataRefresh} from './api';
import {QueryState} from './common';

const clients:QueryClient[]=[];
afterEach(()=>{cleanup();clients.forEach(client=>client.clear());clients.length=0;vi.useRealTimers();focusManager.setFocused(undefined);onlineManager.setOnline(true);vi.unstubAllGlobals();});
function wrapper() {
 const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});clients.push(client);
 return function Provider({children}:{children:ReactNode}) {return <QueryClientProvider client={client}>{children}</QueryClientProvider>;};
}
function response(data:unknown,status=200) {return new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});}
function deferred<T>() {let resolve!:(value:T)=>void;const promise=new Promise<T>(r=>{resolve=r;});return {promise,resolve};}
function Post({id}:{id:number}) {
 const query=useApi(`/posts/${id}`);
 return <QueryState query={query}>{data=><div>{data.post.body}</div>}</QueryState>;
}

describe('API hooks',()=>{
 it('checks analysis for new data every five minutes',async()=>{
  let version=0;const fetch=vi.fn(()=>Promise.resolve(response({version:++version})));vi.stubGlobal('fetch',fetch);
  function Analysis(){const query=useApi('/analysis',{own:'all'});return <span>Analysis {query.data?.version}</span>;}
  vi.useFakeTimers();
  render(<Analysis/>,{wrapper:wrapper()});
  await act(async()=>{await vi.advanceTimersByTimeAsync(1);});
  expect(screen.getByText('Analysis 1')).toBeTruthy();
  await act(async()=>{await vi.advanceTimersByTimeAsync(299998);});
  expect(fetch).toHaveBeenCalledTimes(1);
  await act(async()=>{await vi.advanceTimersByTimeAsync(1);});
  expect(fetch).toHaveBeenCalledTimes(2);
  await act(async()=>{await vi.advanceTimersByTimeAsync(1);});
  expect(screen.getByText('Analysis 2')).toBeTruthy();
 });

 it('reuses recent analysis on reopening and reloads it once stale',async()=>{
  let version=0;const fetch=vi.fn(()=>Promise.resolve(response({version:++version})));vi.stubGlobal('fetch',fetch);
  const params={own:'all'};
  function Analysis(){const query=useApi('/analysis',params);return <span>Analysis {query.data?.version}</span>;}
  const view=render(<Analysis/>,{wrapper:wrapper()});await screen.findByText('Analysis 1');
  view.rerender(<></>);view.rerender(<Analysis/>);
  expect(fetch).toHaveBeenCalledTimes(1);
  view.rerender(<></>);
  clients.at(-1)!.setQueryData(['/analysis',params],{version:1},{updatedAt:Date.now()-60001});
  view.rerender(<Analysis/>);
  expect(await screen.findByText('Analysis 2')).toBeTruthy();
  expect(fetch).toHaveBeenCalledTimes(2);
 });

 it.each(['focus','reconnect'])('reloads stale analysis on %s without refetching a recent result',async event=>{
  let version=0;const fetch=vi.fn(()=>Promise.resolve(response({version:++version})));vi.stubGlobal('fetch',fetch);
  const params={own:'all'},trigger=()=>{if(event==='focus'){focusManager.setFocused(false);focusManager.setFocused(true);}else{onlineManager.setOnline(false);onlineManager.setOnline(true);}};
  function Analysis(){const query=useApi('/analysis',params);return <span>Analysis {query.data?.version}</span>;}
  render(<Analysis/>,{wrapper:wrapper()});await screen.findByText('Analysis 1');
  await act(async()=>trigger());
  expect(fetch).toHaveBeenCalledTimes(1);
  clients.at(-1)!.setQueryData(['/analysis',params],{version:1},{updatedAt:Date.now()-60001});
  await act(async()=>trigger());
  expect(await screen.findByText('Analysis 2')).toBeTruthy();
  expect(fetch).toHaveBeenCalledTimes(2);
 });

 it('lets the first analysis finish while the page is closed and reuses its result',async()=>{
  const pending=deferred<Response>();const fetch=vi.fn(()=>pending.promise);vi.stubGlobal('fetch',fetch);
  function Analysis(){const query=useApi('/analysis',{own:'all'});return <span>{query.data?'Completed analysis':'Loading'}</span>;}
  const view=render(<Analysis/>,{wrapper:wrapper()});await waitFor(()=>expect(fetch).toHaveBeenCalledTimes(1));
  view.rerender(<></>);
  await act(async()=>{pending.resolve(response({selected:1}));await pending.promise;});
  view.rerender(<Analysis/>);
  expect(await screen.findByText('Completed analysis')).toBeTruthy();
  expect(fetch).toHaveBeenCalledTimes(1);
 });

 it('explicitly refreshes the selected scope and invalidates only its other saved views',async()=>{
  const fetch=vi.fn((input:RequestInfo|URL)=>Promise.resolve(response({value:String(input).includes('refresh=true')?'Latest analysis':'Saved analysis'})));
  vi.stubGlobal('fetch',fetch);
  const params={own:'own',sections:'core',include_exportable:false};
  function Workspace(){
   const query=useApi('/analysis',params),refresh=useAnalysisRefresh();
   return <><span>{query.data?.value}</span><button onClick={()=>refresh.mutate(params)}>Refresh analysis</button>{refresh.isSuccess&&<span>Refreshed</span>}</>;
  }
  const provider=wrapper(),client=clients.at(-1)!;
  const sameScope=['/analysis',{...params,sections:'trending',focus_post_id:42,detail_limit:100}],otherScope=['/analysis',{...params,own:'all'}];
  client.setQueryData(sameScope,{value:'Other tab'});client.setQueryData(otherScope,{value:'Other scope'});
  render(<Workspace/>,{wrapper:provider});await screen.findByText('Saved analysis');
  fireEvent.click(screen.getByRole('button',{name:'Refresh analysis'}));await screen.findByText('Refreshed');
  expect(screen.getByText('Latest analysis')).toBeTruthy();
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(fetch.mock.calls[1][0]).toBe('/api/analysis?own=own&sections=core&include_exportable=false&refresh=true');
  expect(client.getQueryState(sameScope)?.isInvalidated).toBe(true);
  expect(client.getQueryState(otherScope)?.isInvalidated).toBe(false);
  expect(client.getQueryState(['/analysis',params])?.isInvalidated).toBe(false);
 });

 it('does not replace a refreshed result when an earlier analysis finishes late',async()=>{
  const previous=deferred<Response>();
  const fetch=vi.fn((input:RequestInfo|URL)=>String(input).includes('refresh=true')?Promise.resolve(response({value:'Latest analysis'})):previous.promise);
  vi.stubGlobal('fetch',fetch);
  const params={own:'all',sections:'core'};
  function Workspace(){const query=useApi('/analysis',params),refresh=useAnalysisRefresh();return <><span>{query.data?.value||'Loading'}</span><button onClick={()=>refresh.mutate(params)}>Refresh analysis</button></>;}
  render(<Workspace/>,{wrapper:wrapper()});await waitFor(()=>expect(fetch).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByRole('button',{name:'Refresh analysis'}));await screen.findByText('Latest analysis');
  await act(async()=>{previous.resolve(response({value:'Old analysis'}));await previous.promise;});
  expect(screen.getByText('Latest analysis')).toBeTruthy();
  expect(screen.queryByText('Old analysis')).toBeNull();
 });

 it('forces the current analysis scope when refreshing local data',async()=>{
  const fetch=vi.fn((input:RequestInfo|URL)=>Promise.resolve(response({value:String(input).includes('refresh=true')?'Latest analysis':'Saved analysis'})));vi.stubGlobal('fetch',fetch);
  const params={own:'own',author_id:'44',sections:'core',include_exportable:false};
  function Workspace(){const query=useApi('/analysis',params),posts=useApi('/posts/1'),refresh=useLocalDataRefresh();return <><span>{query.data?.value}</span><span>{posts.isSuccess?'Posts loaded':''}</span><button onClick={()=>refresh.mutate(params)}>Refresh local data</button>{refresh.isSuccess&&<span>Refreshed</span>}</>;}
  render(<Workspace/>,{wrapper:wrapper()});await screen.findByText('Saved analysis');await screen.findByText('Posts loaded');
  fireEvent.click(screen.getByRole('button',{name:'Refresh local data'}));await screen.findByText('Refreshed');
  expect(screen.getByText('Latest analysis')).toBeTruthy();
  const analysisCalls=fetch.mock.calls.filter(([input])=>String(input).startsWith('/api/analysis?'));
  expect(analysisCalls).toHaveLength(2);
  expect(String(analysisCalls[1][0])).toContain('own=own&author_id=44&sections=core&include_exportable=false&refresh=true');
  expect(fetch.mock.calls.filter(([input])=>input==='/api/posts/1')).toHaveLength(2);
 });

 it('refreshes other pages without starting inactive statistical analysis',async()=>{
  const fetch=vi.fn((_input:RequestInfo|URL)=>Promise.resolve(response({ok:true})));vi.stubGlobal('fetch',fetch);
  const provider=wrapper();clients.at(-1)!.setQueryData(['/analysis',{own:'all'}],{value:'Saved analysis'});
  function Workspace(){const query=useApi('/ai/providers'),refresh=useLocalDataRefresh();return <><span>{query.isSuccess?'Loaded':''}</span><button onClick={()=>refresh.mutate(undefined)}>Refresh local data</button>{refresh.isSuccess&&<span>Refreshed</span>}</>;}
  render(<Workspace/>,{wrapper:provider});await screen.findByText('Loaded');
  fireEvent.click(screen.getByRole('button',{name:'Refresh local data'}));await screen.findByText('Refreshed');
  expect(fetch.mock.calls).toHaveLength(2);
  expect(fetch.mock.calls.every(([input])=>input==='/api/ai/providers')).toBe(true);
 });

 it('reloads a tab opened during refresh and discards its late older response',async()=>{
  const refreshed=deferred<Response>(),olderGrowth=deferred<Response>();let growthCalls=0;
  const fetch=vi.fn((input:RequestInfo|URL)=>{
   const url=String(input);
   if(url.includes('refresh=true'))return refreshed.promise;
   if(url.includes('sections=growth'))return ++growthCalls===1?olderGrowth.promise:Promise.resolve(response({value:'Latest growth'}));
   return Promise.resolve(response({value:'Saved core'}));
  });vi.stubGlobal('fetch',fetch);
  const params={own:'all',sections:'core'};
  function Workspace({section}:{section:string}){
   const query=useApi('/analysis',{...params,sections:section},undefined,true),refresh=useAnalysisRefresh();
   return <><span>{query.data?.value||'Loading'}</span><button onClick={()=>refresh.mutate(params)}>Refresh analysis</button></>;
  }
  const view=render(<Workspace section="core"/>,{wrapper:wrapper()});await screen.findByText('Saved core');
  fireEvent.click(screen.getByRole('button',{name:'Refresh analysis'}));
  await waitFor(()=>expect(fetch.mock.calls.some(([input])=>String(input).includes('refresh=true'))).toBe(true));
  view.rerender(<Workspace section="growth"/>);await waitFor(()=>expect(growthCalls).toBe(1));
  await act(async()=>{refreshed.resolve(response({value:'Latest core'}));await refreshed.promise;});
  expect(await screen.findByText('Latest growth')).toBeTruthy();expect(growthCalls).toBe(2);
  await act(async()=>{olderGrowth.resolve(response({value:'Old growth'}));await olderGrowth.promise;});
  expect(screen.getByText('Latest growth')).toBeTruthy();expect(screen.queryByText('Old growth')).toBeNull();
 });

 it('retains analysis results during linked navigation and replaces them on completion',async()=>{
  const next=deferred<Response>();
  vi.stubGlobal('fetch',vi.fn((input:RequestInfo|URL)=>String(input).includes('focus_post_id=2')?next.promise:Promise.resolve(response({selected:1}))));
  function Analysis({id}:{id:number}) {const query=useApi('/analysis',{focus_post_id:id},60000,true);return <QueryState query={query}>{data=><div>Result {data.selected}{query.isFetching&&<span> Updating</span>}</div>}</QueryState>;}
  const view=render(<Analysis id={1}/>,{wrapper:wrapper()});
  expect(await screen.findByText('Result 1')).toBeTruthy();
  view.rerender(<Analysis id={2}/>);
  expect(screen.getByText('Result 1')).toBeTruthy();
  expect(screen.getByText('Updating')).toBeTruthy();
  await act(async()=>{next.resolve(response({selected:2}));await next.promise;});
  expect(await screen.findByText('Result 2')).toBeTruthy();
  expect(screen.queryByText('Result 1')).toBeNull();
 });

 it.each([200,500])('keeps the selected post when an old request returns status %s late',async status=>{
  const old=deferred<Response>();
  const fetch=vi.fn((input:RequestInfo|URL)=>String(input)==='/api/posts/1'?old.promise:Promise.resolve(response({post:{body:'Current post'}})));
  vi.stubGlobal('fetch',fetch);
  const view=render(<Post id={1}/>,{wrapper:wrapper()});
  await waitFor(()=>expect(fetch).toHaveBeenCalledTimes(1));
  view.rerender(<Post id={2}/>);
  expect(await screen.findByText('Current post')).toBeTruthy();
  await act(async()=>{old.resolve(response(status===200?{post:{body:'Stale post'}}:{error:'Stale error'},status));await old.promise;});
  expect(screen.getByText('Current post')).toBeTruthy();
  expect(screen.queryByText('Stale post')).toBeNull();
  expect(screen.queryByText('Stale error')).toBeNull();
 });

 it('shows the server error and supports retrying the active request',async()=>{
  const fetch=vi.fn().mockResolvedValueOnce(response({error:'帖子不存在'},404)).mockResolvedValueOnce(response({post:{body:'Recovered post'}}));
  vi.stubGlobal('fetch',fetch);
  render(<Post id={42}/>,{wrapper:wrapper()});
  expect(await screen.findByText('帖子不存在')).toBeTruthy();
  fireEvent.click(screen.getByRole('button',{name:/重\s*试/}));
  expect(await screen.findByText('Recovered post')).toBeTruthy();
  expect(fetch.mock.calls.every(call=>call[0]==='/api/posts/42')).toBe(true);
 });

 it('gets a fresh bootstrap token before posting JSON only to the local API',async()=>{
  const fetch=vi.fn().mockResolvedValueOnce(response({csrf_token:'test-token'})).mockResolvedValueOnce(response({saved:true}));
  vi.stubGlobal('fetch',fetch);
  const payload={account_ids:[12],team_label:'Example',evidence:'Verified locally'};
  function Save() {const action=useAction('/team-accounts');return <><button onClick={()=>action.mutate(payload)}>Save</button>{action.isSuccess&&<span>Saved</span>}</>;}
  render(<Save/>,{wrapper:wrapper()});
  fireEvent.click(screen.getByRole('button',{name:'Save'}));
  expect(await screen.findByText('Saved')).toBeTruthy();
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(fetch.mock.calls[0][0]).toBe('/api/bootstrap');
  expect(fetch.mock.calls[1]).toEqual(['/api/team-accounts',{method:'POST',headers:{'Content-Type':'application/json','X-CTN-Token':'test-token'},body:JSON.stringify(payload)}]);
 });

 it.each([
  ['/ai/preview',1,1],['/ai/chat',1,1],['/ai/run',1,1],['/ai/providers/test',1,1],
  ['/ai/providers/save',1,2],['/ai/providers/delete',1,2],['/analysis/run',1,1],['/team-accounts',2,2],
 ])('refreshes only changed resources after %s',async(path,expectedAnalysis,expectedProviders)=>{
  const fetch=vi.fn((input:RequestInfo|URL)=>Promise.resolve(response(String(input)==='/api/bootstrap'?{csrf_token:'test-token'}:{ok:true})));
  vi.stubGlobal('fetch',fetch);
  function Workspace(){
   const analysis=useApi('/analysis',{own:'all'},60000),providers=useApi('/ai/providers',{},60000),posts=useApi('/posts/1',{},60000),action=useAction(String(path));
   return <><span>{analysis.isSuccess&&providers.isSuccess&&posts.isSuccess?'Loaded':'Loading'}</span><button onClick={()=>action.mutate({})}>Run action</button>{action.isSuccess&&<span>Done</span>}</>;
  }
  render(<Workspace/>,{wrapper:wrapper()});await screen.findByText('Loaded');
  fireEvent.click(screen.getByRole('button',{name:'Run action'}));await screen.findByText('Done');
  const calls=(url:string)=>fetch.mock.calls.filter(([input])=>String(input)===url).length;
  expect(calls('/api/analysis?own=all')).toBe(expectedAnalysis);
  expect(calls('/api/ai/providers')).toBe(expectedProviders);
  expect(calls('/api/posts/1')).toBe(expectedAnalysis);
 });
});
