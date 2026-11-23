import {Link, useLocation} from 'react-router-dom';
import {useQueryClient} from '@tanstack/react-query';
import type {Row} from '../api';
import {breadcrumbs} from './navConfig';

// Role names come from an already loaded /agents result; no extra request is made for the breadcrumb.
function useAgentName() {
 const client=useQueryClient();
 return (id:string)=>{
  for(const [,state] of client.getQueriesData<Row>({queryKey:['/agents']})){
   const agent=Array.isArray(state?.agents)?state.agents.find((row:Row)=>String(row.id)===id):undefined;
   if(agent?.name)return String(agent.name);
  }
  return undefined;
 };
}

export default function Breadcrumbs() {
 const location=useLocation(),crumbs=breadcrumbs(location.pathname,location.search,useAgentName());
 if(!crumbs.length)return null;
 return <nav className="breadcrumbs" aria-label="当前位置"><ol>{crumbs.map((crumb,index)=><li key={index}>
  {crumb.to?<Link to={crumb.to}>{crumb.label}</Link>:<span aria-current={index===crumbs.length-1?'page':undefined}>{crumb.label}</span>}
 </li>)}</ol></nav>;
}
