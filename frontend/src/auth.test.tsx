// @vitest-environment jsdom
import {StrictMode} from 'react';
import {afterEach, describe, expect, it, vi} from 'vitest';
import {act, cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import {AuthGate, UserMenu} from './auth';
import {request} from './api';

const user={name:'Team Member',email:'member@example.test'};
const clients:QueryClient[]=[];
function response(data:unknown,status=200) {return new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});}
function setup(strict=false) {
 const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});clients.push(client);
 const tree=<QueryClientProvider client={client}><AuthGate><UserMenu/><div>Private workspace</div></AuthGate></QueryClientProvider>;
 render(strict?<StrictMode>{tree}</StrictMode>:tree);return client;
}
function fillLogin() {
 fireEvent.change(screen.getByLabelText('邮箱'),{target:{value:'member@example.test'}});
 fireEvent.change(screen.getByLabelText('密码'),{target:{value:'sample-secret'}});
 fireEvent.submit(screen.getByRole('form',{name:'登录'}));
}
afterEach(()=>{cleanup();clients.forEach(client=>client.clear());clients.length=0;vi.unstubAllGlobals();window.history.replaceState({},'','/');});

describe('workspace authentication',()=>{
 it('restores a session before mounting protected content, including StrictMode',async()=>{
  let resolve!:(value:Response)=>void;
  const fetch=vi.fn(()=>new Promise<Response>(done=>{resolve=done;}));vi.stubGlobal('fetch',fetch);
  setup(true);
  expect(screen.queryByText('Private workspace')).toBeNull();
  expect(screen.getByRole('status').textContent).toContain('正在恢复');
  await act(async()=>resolve(response({user,csrf_token:'restored-token'})));
  expect(await screen.findByText('Private workspace')).toBeTruthy();
  expect(screen.getByText(user.email)).toBeTruthy();
 });

 it('logs in after a backend token rotation and preserves a deep link',async()=>{
  window.history.replaceState({},'','/analysis?focus_post_id=17');
  const fetch=vi.fn().mockResolvedValueOnce(response({user:null,csrf_token:'stale-token'})).mockResolvedValueOnce(response({user:null,csrf_token:'new-server-token'})).mockResolvedValueOnce(response({user,csrf_token:'new-server-token'}));vi.stubGlobal('fetch',fetch);
  const client=setup();client.setQueryData(['old-user-data'],{private:true});
  await screen.findByLabelText('邮箱');fillLogin();
  expect(await screen.findByText('Private workspace')).toBeTruthy();
  expect(fetch.mock.calls[1][0]).toBe('/api/auth/session');
  expect(fetch.mock.calls[2]).toEqual(['/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json','X-CTN-Token':'new-server-token'},body:JSON.stringify({email:user.email,password:'sample-secret'})}]);
  expect(client.getQueryData(['old-user-data'])).toBeUndefined();
  expect(window.location.pathname+window.location.search).toBe('/analysis?focus_post_id=17');
 });

 it('keeps an invalid login on the form and allows retry',async()=>{
  const fetch=vi.fn().mockResolvedValueOnce(response({user:null,csrf_token:'anonymous-token'})).mockResolvedValueOnce(response({user:null,csrf_token:'anonymous-token'})).mockResolvedValueOnce(response({detail:'邮箱或密码错误'},401)).mockResolvedValueOnce(response({user:null,csrf_token:'anonymous-token'})).mockResolvedValueOnce(response({user,csrf_token:'signed-token'}));vi.stubGlobal('fetch',fetch);
  setup();await screen.findByLabelText('邮箱');fillLogin();
  expect(await screen.findByRole('alert')).toHaveProperty('textContent','邮箱或密码错误');
  expect(screen.queryByText('Private workspace')).toBeNull();
  fillLogin();expect(await screen.findByText('Private workspace')).toBeTruthy();
  expect(fetch).toHaveBeenCalledTimes(5);
 });

 it('logs out after a backend restart, removes cached data and returns to the login form',async()=>{
  const fetch=vi.fn().mockResolvedValueOnce(response({user,csrf_token:'stale-token'})).mockResolvedValueOnce(response({user:null,csrf_token:'new-server-token'})).mockResolvedValueOnce(response({user:null,csrf_token:'new-server-token'}));vi.stubGlobal('fetch',fetch);
  const client=setup();await screen.findByText('Private workspace');client.setQueryData(['sensitive-records'],['private']);
  fireEvent.click(screen.getByRole('button',{name:'退出登录'}));
  expect(await screen.findByLabelText('邮箱')).toBeTruthy();
  expect(screen.queryByText('Private workspace')).toBeNull();
  expect(client.getQueryCache().getAll()).toHaveLength(0);
  expect(fetch.mock.calls[1][0]).toBe('/api/auth/session');
  expect(fetch.mock.calls[2]).toEqual(['/api/auth/logout',{method:'POST',headers:{'Content-Type':'application/json','X-CTN-Token':'new-server-token'},body:'{}'}]);
 });

 it.each(['login','logout'])('keeps a late 401 from interrupting an active %s',async action=>{
  let resolve!:(value:Response)=>void;
  const pending=new Promise<Response>(done=>{resolve=done;});
  const fetch=vi.fn().mockResolvedValueOnce(response({user:action==='logout'?user:null,csrf_token:'old-token'})).mockReturnValueOnce(pending).mockResolvedValueOnce(response({user:action==='login'?user:null,csrf_token:'current-token'}));vi.stubGlobal('fetch',fetch);
  setup();
  if(action==='login'){await screen.findByLabelText('邮箱');fillLogin();}
  else {await screen.findByText('Private workspace');fireEvent.click(screen.getByRole('button',{name:'退出登录'}));}
  await waitFor(()=>expect(fetch).toHaveBeenCalledTimes(2));
  act(()=>window.dispatchEvent(new Event('ctn:unauthorized')));
  expect(fetch).toHaveBeenCalledTimes(2);
  await act(async()=>{resolve(response({user:action==='logout'?user:null,csrf_token:'fresh-token'}));await pending;});
  if(action==='login')expect(await screen.findByText('Private workspace')).toBeTruthy();
  else expect(await screen.findByLabelText('邮箱')).toBeTruthy();
  expect(fetch).toHaveBeenCalledTimes(3);
  expect(fetch.mock.calls[2][0]).toBe('/api/auth/'+action);
  expect(screen.queryByText('登录已过期，请重新登录。')).toBeNull();
 });

 it('clears protected data only after a 401 is confirmed by the current session',async()=>{
  let resolve!:(value:Response)=>void;
  const pending=new Promise<Response>(done=>{resolve=done;});
  const fetch=vi.fn().mockResolvedValueOnce(response({user,csrf_token:'signed-token'})).mockResolvedValueOnce(response({detail:'登录已过期'},401)).mockReturnValueOnce(pending);vi.stubGlobal('fetch',fetch);
  const client=setup();await screen.findByText('Private workspace');client.setQueryData(['sensitive-records'],['private']);
  await act(async()=>{await expect(request('/posts')).rejects.toThrow('登录已过期');});
  expect(screen.getByText('Private workspace')).toBeTruthy();
  expect(client.getQueryData(['sensitive-records'])).toEqual(['private']);
  await act(async()=>{resolve(response({user:null,csrf_token:'new-token'}));await pending;});
  expect(await screen.findByLabelText('邮箱')).toBeTruthy();
  expect(screen.queryByText('Private workspace')).toBeNull();
  expect(client.getQueryCache().getAll()).toHaveLength(0);
  expect(screen.getByText('登录已过期，请重新登录。')).toBeTruthy();
  expect(fetch.mock.calls[2][0]).toBe('/api/auth/session');
 });

 it('coalesces duplicate 401 checks and preserves a valid session and its data',async()=>{
  let resolve!:(value:Response)=>void;
  const pending=new Promise<Response>(done=>{resolve=done;});
  const fetch=vi.fn().mockResolvedValueOnce(response({user,csrf_token:'signed-token'})).mockReturnValueOnce(pending);vi.stubGlobal('fetch',fetch);
  const client=setup();await screen.findByText('Private workspace');client.setQueryData(['saved-view'],['keep']);
  act(()=>{window.dispatchEvent(new Event('ctn:unauthorized'));window.dispatchEvent(new Event('ctn:unauthorized'));});
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(screen.getByText('Private workspace')).toBeTruthy();
  await act(async()=>{resolve(response({user,csrf_token:'current-token'}));await pending;});
  expect(screen.getByText('Private workspace')).toBeTruthy();
  expect(client.getQueryData(['saved-view'])).toEqual(['keep']);
  expect(screen.queryByText('登录已过期，请重新登录。')).toBeNull();
 });

 it.each(['network','server','invalid response'])('preserves the session after a %s failure and allows another confirmation',async failure=>{
  const fetch=vi.fn().mockResolvedValueOnce(response({user,csrf_token:'signed-token'}));
  if(failure==='network')fetch.mockRejectedValueOnce(new Error('网络连接失败'));
  else if(failure==='server')fetch.mockResolvedValueOnce(response({error:'服务暂时不可用'},503));
  else fetch.mockResolvedValueOnce(response({}));
  fetch.mockResolvedValueOnce(response({user:null,csrf_token:'new-token'}));vi.stubGlobal('fetch',fetch);
  const client=setup();await screen.findByText('Private workspace');client.setQueryData(['saved-view'],['keep']);
  await act(async()=>{window.dispatchEvent(new Event('ctn:unauthorized'));});
  expect(screen.getByText('Private workspace')).toBeTruthy();
  expect(client.getQueryData(['saved-view'])).toEqual(['keep']);
  act(()=>window.dispatchEvent(new Event('ctn:unauthorized')));
  expect(await screen.findByLabelText('邮箱')).toBeTruthy();
  expect(fetch).toHaveBeenCalledTimes(3);
  expect(client.getQueryCache().getAll()).toHaveLength(0);
 });

 it('ignores a session confirmation that finishes after logout starts',async()=>{
  let resolveCheck!:(value:Response)=>void,resolveToken!:(value:Response)=>void;
  const pendingCheck=new Promise<Response>(done=>{resolveCheck=done;});
  const pendingToken=new Promise<Response>(done=>{resolveToken=done;});
  const fetch=vi.fn().mockResolvedValueOnce(response({user,csrf_token:'signed-token'})).mockReturnValueOnce(pendingCheck).mockReturnValueOnce(pendingToken).mockResolvedValueOnce(response({user:null,csrf_token:'current-token'}));vi.stubGlobal('fetch',fetch);
  const client=setup();await screen.findByText('Private workspace');client.setQueryData(['saved-view'],['keep']);
  act(()=>window.dispatchEvent(new Event('ctn:unauthorized')));
  expect(fetch).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByRole('button',{name:'退出登录'}));
  await act(async()=>{resolveCheck(response({user:null,csrf_token:'obsolete-token'}));await pendingCheck;});
  expect(screen.getByText('Private workspace')).toBeTruthy();
  expect(client.getQueryData(['saved-view'])).toEqual(['keep']);
  expect(screen.getByRole('button',{name:'正在退出…'})).toHaveProperty('disabled',true);
  await act(async()=>{resolveToken(response({user,csrf_token:'fresh-token'}));await pendingToken;});
  expect(fetch.mock.calls.map(call=>call[0])).toEqual(['/api/auth/session','/api/auth/session','/api/auth/session','/api/auth/logout']);
  expect(await screen.findByLabelText('邮箱')).toBeTruthy();
  expect(screen.getByText('已退出登录。')).toBeTruthy();
  expect(fetch.mock.calls[3][0]).toBe('/api/auth/logout');
 });

 it('does not restore an old confirmation after explicit logout completes',async()=>{
  let resolve!:(value:Response)=>void;
  const pending=new Promise<Response>(done=>{resolve=done;});
  const fetch=vi.fn().mockResolvedValueOnce(response({user,csrf_token:'signed-token'})).mockReturnValueOnce(pending).mockResolvedValueOnce(response({user,csrf_token:'fresh-token'})).mockResolvedValueOnce(response({user:null,csrf_token:'signed-out-token'}));vi.stubGlobal('fetch',fetch);
  setup();await screen.findByText('Private workspace');
  act(()=>window.dispatchEvent(new Event('ctn:unauthorized')));
  expect(fetch).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByRole('button',{name:'退出登录'}));
  await screen.findByLabelText('邮箱');
  await act(async()=>{resolve(response({user,csrf_token:'obsolete-token'}));await pending;});
  expect(screen.getByLabelText('邮箱')).toBeTruthy();
  expect(screen.queryByText('Private workspace')).toBeNull();
  expect(screen.getByText('已退出登录。')).toBeTruthy();
 });

 it('offers a connection retry when session restoration fails',async()=>{
  const fetch=vi.fn().mockRejectedValueOnce(new Error('网络连接失败')).mockResolvedValueOnce(response({user:null,csrf_token:'retry-token'}));vi.stubGlobal('fetch',fetch);
  setup();expect(await screen.findByRole('alert')).toHaveProperty('textContent','网络连接失败');
  fireEvent.click(screen.getByRole('button',{name:'重新连接'}));
  await waitFor(()=>expect(screen.getByLabelText('邮箱')).toBeTruthy());
  expect(screen.queryByRole('alert')).toBeNull();
 });
});
