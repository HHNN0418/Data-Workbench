import type {ReactNode} from 'react';
import {Link, useNavigate} from 'react-router-dom';
import {Button, Collapse, Dropdown, Empty} from 'antd';
import {DownOutlined} from '@ant-design/icons';
import './ui.css';

// The page title comes from the shell; a page only adds its buttons and an optional note (§13.3).
export function PageHeader({note,actions}:{note?:ReactNode;actions?:ReactNode}) {
 if(!note&&!actions)return null;
 return <div className="page-header"><div className="page-header-note">{note}</div>{actions&&<div className="page-header-actions">{actions}</div>}</div>;
}

export interface MoreItem {key:string;label:ReactNode;to?:string;href?:string;onClick?:()=>void;danger?:boolean;disabled?:boolean}
// Low-frequency actions (export, back-fill, raw records) share one menu instead of taking the first screen.
export function MoreMenu({items,label='更多'}:{items:MoreItem[];label?:string}) {
 const navigate=useNavigate();
 return <Dropdown trigger={['click']} menu={{items:items.map(item=>({key:item.key,danger:item.danger,disabled:item.disabled,label:item.href?<a href={item.href}>{item.label}</a>:item.label})),onClick:({key})=>{const item=items.find(row=>row.key===key);if(!item||item.href)return;if(item.to)navigate(item.to);else item.onClick?.();}}}>
  <Button>{label} <DownOutlined/></Button>
 </Dropdown>;
}

// Method notes, detail tables and raw data live at the bottom of a page, closed by default.
export function Advanced({title='高级 / 导出',children,defaultOpen=false}:{title?:ReactNode;children:ReactNode;defaultOpen?:boolean}) {
 return <Collapse ghost className="advanced" defaultActiveKey={defaultOpen?['advanced']:[]} items={[{key:'advanced',label:title,children}]}/>;
}

export type NextAction={label:string;to?:string;onClick?:()=>void};
// Empty states say what to do next (§4.5).
export function NextStep({title,description,action}:{title:ReactNode;description?:ReactNode;action?:NextAction}) {
 return <Empty className="next-step" image={Empty.PRESENTED_IMAGE_SIMPLE} description={<><strong>{title}</strong>{description&&<span className="muted">{description}</span>}</>}>
  {action&&(action.to?<Link to={action.to}>{action.label}</Link>:<Button onClick={action.onClick}>{action.label}</Button>)}
 </Empty>;
}

// A readable title and next step for a failed request; request() puts the HTTP status in the message.
export function errorHint(error:Error|null|undefined):{title:string;description:string;action?:NextAction} {
 const text=error?.message||'未知错误',status=Number(text.match(/\((\d{3})\)/)?.[1]);
 if(status===503||/维护/.test(text))return {title:'系统维护中',description:'维护完成后会自动恢复，稍后重试即可。'};
 if(status===403||/csrf|令牌/i.test(text))return {title:'页面令牌已过期',description:'刷新页面后再试一次。',action:{label:'刷新页面',onClick:()=>window.location.reload()}};
 if(status===502||status===504||/Failed to fetch|NetworkError|Load failed/i.test(text))return {title:'后端暂时没有响应',description:'可能正在重载或网络中断，稍后重试。连续失败时查看采集与运行。',action:{label:'查看采集与运行',to:'/operations'}};
 if(status===404)return {title:'接口还不可用',description:`${text}。这个功能的后端可能还没上线。`};
 return {title:'读取失败',description:text};
}
