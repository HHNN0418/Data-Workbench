import {lazy, Suspense, useMemo, useState, type ReactNode} from 'react';
import {Alert, Button, Empty, Skeleton, Table, Tag} from 'antd';
import type {ChartProps} from './Chart';
import type {Row} from './api';
import {labels} from './labels';
export const label=(key:string)=>labels[key]||key;
const numberFormatter=new Intl.NumberFormat('zh-CN',{maximumFractionDigits:4});
const timeFormatter=new Intl.DateTimeFormat('zh-CN',{timeZone:'Australia/Sydney',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
export function fmt(value:unknown):string {if(value===null||value===undefined||value==='')return '—';if(typeof value==='boolean')return value?'是':'否';if(typeof value==='number')return Number.isFinite(value)?numberFormatter.format(value):'—';return typeof value==='object'?JSON.stringify(value):String(value)}
export function time(value:unknown):string {if(!value)return '—';const date=new Date(String(value));return Number.isNaN(date.valueOf())?String(value):timeFormatter.format(date)}
export function Panel({title,extra,children}:{title:ReactNode;extra?:ReactNode;children:ReactNode}) {return <section className="panel"><div className="panel-head"><h2>{title}</h2>{extra}</div>{children}</section>}
export function Details({data,title='完整字段'}:{data:unknown;title?:string}) {
 const [expanded,setExpanded]=useState(false);
 const text=useMemo(()=>expanded?JSON.stringify(data,null,2):undefined,[data,expanded]);
 return <details className="details" onToggle={event=>setExpanded(event.currentTarget.open)}><summary>{title}</summary>{expanded&&<pre>{text}</pre>}</details>;
}
export function Metrics({items}:{items:Array<{label:ReactNode;value:unknown;hint?:ReactNode}>}) {return <div className="metrics">{items.map((item,i)=><div className="metric" key={i}><div className="metric-label">{item.label}</div><strong>{fmt(item.value)}</strong>{item.hint&&<span>{item.hint}</span>}</div>)}</div>}
export function QueryState<T>({query,children}:{query:{isPending:boolean;isError:boolean;error:Error|null;data:T|undefined;refetch:()=>unknown};children:(data:T)=>ReactNode}) {if(query.isPending)return <Skeleton active paragraph={{rows:5}}/>;if(query.isError)return <Alert type="error" showIcon title="读取失败" description={query.error?.message} action={<Button onClick={()=>query.refetch()}>重试</Button>}/>;if(query.data===undefined)return <Empty/>;return <>{children(query.data)}</>}
export function valueCell(value:unknown,key:string):ReactNode {
 if(value===null||value===undefined||value==='')return <span className="muted">—</span>;
 if(key==='id'||key.endsWith('_id'))return String(value);
 if(key.endsWith('_at')||['first_seen','last_seen','from','to'].includes(key))return time(value);
 if(Array.isArray(value)&&!value.length)return <span className="muted">—</span>;
 if(typeof value==='object')return <Details data={value} title="展开"/>;
 if(['status','parse_status','kind','track'].includes(key))return <Tag>{label(String(value))}</Tag>;
 if(['metric','x','y','relation','event','direction'].includes(key))return label(String(value));
 if(key==='is_own')return value===1||value===true?'本队':'归属未知';
 return fmt(value);
}
export function DataTable({rows,columns,pageSize=10}:{rows:Row[];columns?:string[];pageSize?:number}) {
 // Stringifying every row for its key was slow on thousands of rows; rows are display-only.
 const rowKeys=useMemo(()=>new WeakMap(rows.map((row,index)=>[row,String(index)])),[rows]);
 const keys=columns||Array.from(new Set(rows.flatMap(Object.keys))).slice(0,8);
 return <Table size="middle" dataSource={rows} rowKey={row=>rowKeys.get(row)!} scroll={{x:'max-content'}} pagination={rows.length>pageSize?{pageSize,showSizeChanger:false,showTotal:n=>`${n} 条`}:false} columns={keys.map(key=>({title:label(key),dataIndex:key,key,render:(v:unknown)=>valueCell(v,key)}))}/>;
}
const LazyChart=lazy(()=>import('./Chart'));
export function Chart(props:ChartProps) {return <Suspense fallback={<Skeleton active style={{height:props.height||280}}/>}><LazyChart {...props}/></Suspense>}
export function downloadJSON(data:unknown,name:string) {const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
