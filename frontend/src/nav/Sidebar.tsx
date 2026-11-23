import {useEffect, useState} from 'react';
import {Link, useLocation} from 'react-router-dom';
import {DownOutlined, ExportOutlined, RightOutlined} from '@ant-design/icons';
import {matchNav, navGroups, navItems, type NavGroup} from './navConfig';
import {statusText} from '../pages/pipelineModel';

// Stage health from /api/pipeline/status, keyed by sidebar group ('overview' carries the overall status).
export type SidebarStatus=Record<string,{status:string;headline?:string}>;

function StatusDot({value,announce=false}:{value?:{status:string;headline?:string};announce?:boolean}) {
 if(!value)return null;
 const text=statusText[value.status]||value.status;
 // Inside a link the dot stays out of the link name; on a group heading it is read with the group name.
 return announce
  ?<span className="sidebar-dot" data-status={value.status} role="img" aria-label={`状态：${text}`} title={value.headline||text}/>
  :<span className="sidebar-dot" data-status={value.status} aria-hidden="true" title={value.headline||text}/>;
}

function GroupHeading({group,open,onToggle,status}:{group:NavGroup;open:boolean;onToggle:()=>void;status?:SidebarStatus}) {
 const id=`nav-group-${group.key}`,text=<>{group.step&&<span className="sidebar-step" aria-hidden="true">{group.step}</span>}{group.label}<StatusDot value={status?.[group.key]} announce/></>;
 if(!group.label)return null;
 return group.collapsible
  ?<button type="button" className="sidebar-group-label sidebar-group-toggle" id={id} aria-expanded={open} onClick={onToggle}>{text}{open?<DownOutlined aria-hidden="true"/>:<RightOutlined aria-hidden="true"/>}</button>
  :<div className="sidebar-group-label" id={id}>{text}</div>;
}

export default function Sidebar({onNavigate,status}:{onNavigate?:()=>void;status?:SidebarStatus}) {
 const location=useLocation(),active=matchNav(location.pathname,location.search);
 // Collapsed groups (System) open by themselves when the current page is inside them.
 const [open,setOpen]=useState<Record<string,boolean>>({});
 useEffect(()=>{if(active)setOpen(state=>state[active.group]?state:{...state,[active.group]:true});},[active?.group]);
 return <nav className="sidebar-nav" aria-label="主导航">{navGroups.map(group=>{
  const expanded=!group.collapsible||!!open[group.key],items=navItems.filter(item=>item.group===group.key);
  return <div className={`sidebar-group sidebar-group-${group.key}`} key={group.key}>
   <GroupHeading group={group} open={expanded} status={status} onToggle={()=>setOpen(state=>({...state,[group.key]:!expanded}))}/>
   {expanded&&<ul aria-labelledby={group.label?`nav-group-${group.key}`:undefined}>{items.map(item=><li key={item.key}>
    {item.external
     ?<a href={item.to} target="_blank" rel="noreferrer" title={item.description}>{item.label}<ExportOutlined aria-hidden="true"/></a>
     :<Link to={item.to} title={item.description} className={active?.key===item.key?'active':undefined} aria-current={active?.key===item.key?'page':undefined} onClick={onNavigate}>{item.label}{item.key==='cockpit'&&<StatusDot value={status?.overview}/>}</Link>}
   </li>)}</ul>}
  </div>;
 })}</nav>;
}
