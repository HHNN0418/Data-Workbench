import {useEffect, useState, type ReactNode} from 'react';
import {Link, useLocation} from 'react-router-dom';
import {App, Button, Drawer} from 'antd';
import {MenuOutlined, ReloadOutlined} from '@ant-design/icons';
import {UserMenu} from '../auth';
import {useApi, useLocalDataRefresh, type Row} from '../api';
import {analysisRequestParams, readAnalysisParams} from '../pages/analysisModel';
import {sidebarStatus} from '../pages/pipelineModel';
import Sidebar from './Sidebar';
import Breadcrumbs from './Breadcrumbs';
import {pageTitle} from './navConfig';
import './nav.css';

function Brand() {
 return <Link className="brand" to="/"><span className="brand-mark">c<span>t</span>n</span><span className="brand-name">数据工作台<small>CAPTURE THE NARRATIVE</small></span></Link>;
}

export default function AppShell({children}:{children:ReactNode}) {
 const location=useLocation(),refresh=useLocalDataRefresh(),{message}=App.useApp(),[menuOpen,setMenuOpen]=useState(false);
 const params=readAnalysisParams(new URLSearchParams(location.search)),title=pageTitle(location.pathname,location.search);
 // Same query as the home page, so the dots and the pipeline view share one request a minute.
 const status=sidebarStatus(useApi<Row>('/pipeline/status',{},60000).data);
 useEffect(()=>setMenuOpen(false),[location.pathname,location.search]);
 function reload() {refresh.mutate(location.pathname==='/analysis'&&params.analysis_tab!=='ai'?analysisRequestParams(params):undefined,{onError:error=>void message.error(error.message)});}
 return <><a className="skip-link" href="#main">跳至主要内容</a>
  <div className="shell">
   <aside className="shell-sidebar"><Brand/><Sidebar status={status}/></aside>
   <div className="shell-body">
    <header className="shell-header">
     <Button className="shell-menu-button" type="text" icon={<MenuOutlined/>} aria-label="打开导航" aria-expanded={menuOpen} onClick={()=>setMenuOpen(true)}/>
     <div className="shell-header-brand"><Brand/></div>
     <div className="shell-header-actions"><Button icon={<ReloadOutlined/>} loading={refresh.isPending} onClick={reload} title="重新读取本页数据">刷新</Button><UserMenu/></div>
    </header>
    <main id="main" key={location.pathname}><Breadcrumbs/>{title&&<h1 className="shell-title">{title}</h1>}{children}</main>
    <footer className="shell-footer"><span>CTN / Agent Zero</span><span>显示时间：Australia/Sydney</span></footer>
   </div>
  </div>
  <Drawer className="shell-drawer" placement="left" size={272} open={menuOpen} onClose={()=>setMenuOpen(false)} title="导航"><Sidebar status={status} onNavigate={()=>setMenuOpen(false)}/></Drawer>
 </>;
}
