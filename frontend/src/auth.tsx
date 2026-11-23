import {createContext, useContext, useEffect, useRef, useState, type FormEvent, type ReactNode} from 'react';
import {useQueryClient} from '@tanstack/react-query';
import {request} from './api';
import './auth.css';

interface User {name:string; email:string}
interface Session {user:User|null; csrf_token:string}
interface AuthContextValue {user:User; logout:()=>Promise<void>; busy:boolean; error:string}
const AuthContext=createContext<AuthContextValue|null>(null);
const errorMessage=(error:unknown)=>error instanceof Error?error.message:'请求失败，请稍后重试。';

export function AuthGate({children}:{children:ReactNode}) {
 const client=useQueryClient();
 const [session,setSession]=useState<Session|null>(null);
 const [restoring,setRestoring]=useState(true);
 const [busy,setBusy]=useState(false);
 const [error,setError]=useState('');
 const [notice,setNotice]=useState('');
 const [attempt,setAttempt]=useState(0);
 const generation=useRef(0);
 const operation=useRef(false);
 function clearData() {void client.cancelQueries();client.clear();}

 useEffect(()=>{
  const controller=new AbortController();
  const current=++generation.current;
  setRestoring(true);
  request<Session>('/auth/session',{signal:controller.signal}).then(data=>{
   if(current===generation.current)setSession(data);
  }).catch(reason=>{
   if(!controller.signal.aborted&&current===generation.current)setError(errorMessage(reason));
  }).finally(()=>{
   if(!controller.signal.aborted&&current===generation.current)setRestoring(false);
  });
  return ()=>controller.abort();
 },[attempt]);

 useEffect(()=>{
  let confirmation:AbortController|null=null;
  function expire() {
   // A business API or a request started before login may also return 401.
   // Verify the current cookie before removing the user's page and cached data.
   if(!session?.user||operation.current||confirmation)return;
   const controller=new AbortController();
   confirmation=controller;
   const current=generation.current;
   request<Session>('/auth/session',{signal:controller.signal}).then(data=>{
    if(controller.signal.aborted||current!==generation.current||operation.current)return;
    if(data.user!==null&&!data.user?.email)return;
    if(!data.user||data.user.email!==session.user?.email){
     void client.cancelQueries();
     client.clear();
    }
    setSession(data);
    if(!data.user){
     ++generation.current;
     setError('');
     setNotice('登录已过期，请重新登录。');
    }
   }).catch(()=>{
    // A failed connection does not prove expiry; later 401s can retry.
   }).finally(()=>{if(confirmation===controller)confirmation=null;});
  }
  window.addEventListener('ctn:unauthorized',expire);
  return ()=>{window.removeEventListener('ctn:unauthorized',expire);confirmation?.abort();};
 },[client,session?.user]);

 async function login(event:FormEvent<HTMLFormElement>) {
  event.preventDefault();
  if(!session||operation.current)return;
  const fields=new FormData(event.currentTarget);
  const email=String(fields.get('email')||'').trim();
  const password=String(fields.get('password')||'');
  if(!email||!password){setError('请输入邮箱和密码。');return;}
  operation.current=true;
  const current=++generation.current;
  setBusy(true);setError('');setNotice('');
  try {
   const fresh=await request<Session>('/auth/session');
   if(current!==generation.current)return;
   const data=await request<Session>('/auth/login',{method:'POST',headers:{'Content-Type':'application/json','X-CTN-Token':fresh.csrf_token},body:JSON.stringify({email,password})});
   if(current===generation.current){clearData();setSession(data);}
  } catch(reason) {
   if(current===generation.current)setError(errorMessage(reason));
  } finally {
   if(current===generation.current){setBusy(false);operation.current=false;}
  }
 }

 async function logout() {
  if(!session||operation.current)return;
  operation.current=true;
  const current=++generation.current;
  setBusy(true);setError('');
  try {
   const fresh=await request<Session>('/auth/session');
   if(current!==generation.current)return;
   const data=await request<Session>('/auth/logout',{method:'POST',headers:{'Content-Type':'application/json','X-CTN-Token':fresh.csrf_token},body:'{}'});
   if(current===generation.current){clearData();setSession(data);setNotice('已退出登录。');}
  } catch(reason) {
   if(current===generation.current)setError(errorMessage(reason));
  } finally {
   if(current===generation.current){setBusy(false);operation.current=false;}
  }
 }

 if(session?.user&&!restoring)return <AuthContext.Provider value={{user:session.user,logout,busy,error}}>{children}</AuthContext.Provider>;
 return <main className="auth-page"><section className="auth-card" aria-labelledby="login-heading">
  <div className="auth-brand"><span className="brand-mark">c<span>t</span>n</span><span>CAPTURE THE NARRATIVE</span></div>
  <span className="auth-eyebrow">数据工作台</span><h1 id="login-heading">登录工作区</h1>
  <p className="auth-description">使用团队邮箱登录，继续查看和管理数据。</p>
  {notice&&<p className="auth-notice" role="status">{notice}</p>}
  {error&&<p className="auth-error" role="alert">{error}</p>}
  {restoring?<p className="auth-loading" role="status">正在恢复登录状态…</p>:!session?<button className="auth-submit" onClick={()=>{setError('');setAttempt(value=>value+1);}}>重新连接</button>:<form onSubmit={event=>void login(event)} aria-label="登录" aria-busy={busy}>
   <label htmlFor="login-email">邮箱</label><input id="login-email" name="email" type="email" autoComplete="username" autoCapitalize="none" spellCheck={false} placeholder="请输入团队邮箱" required disabled={busy}/>
   <label htmlFor="login-password">密码</label><input id="login-password" name="password" type="password" autoComplete="current-password" placeholder="请输入密码" required disabled={busy}/>
   <button className="auth-submit" type="submit" disabled={busy}>{busy?'正在登录…':'登录'}</button>
  </form>}
  <p className="auth-footnote">仅限已授权团队成员访问</p>
 </section></main>;
}

export function UserMenu() {
 const auth=useContext(AuthContext);
 if(!auth)return null;
 return <div className="auth-user"><div className="auth-user-details"><strong>{auth.user.name}</strong><span>{auth.user.email}</span></div><button className="auth-logout" disabled={auth.busy} onClick={()=>void auth.logout()}>{auth.busy?'正在退出…':'退出登录'}</button>{auth.error&&<span className="auth-user-error" role="alert">{auth.error}</span>}</div>;
}
