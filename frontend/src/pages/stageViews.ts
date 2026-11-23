import {num} from './pipelineModel';
import {concernLabels} from '../labels';

// Shared wording for the read-only stage pages (processing, strategy, execution, results, system).
export const actionLabels:Record<string,string>={post:'原创',reply:'回复',repost:'转发',like:'点赞',follow:'关注',unfollow:'取消关注',other:'其他'};
export const stanceLabels:Record<string,string>={yes:'偏 Yes',no:'偏 No',undecided:'犹豫',unrelated:'无关'};
export const concernLabel=(key:unknown)=>key===null||key===undefined||key==='none'?'未记录':concernLabels[String(key)]||String(key);

const hourFormatter=new Intl.DateTimeFormat('zh-CN',{timeZone:'Australia/Sydney',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
const clockFormatter=new Intl.DateTimeFormat('zh-CN',{timeZone:'Australia/Sydney',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
// "10/22 19:47" in Sydney time; the pages say the time zone once.
export function sydney(value:unknown):string {if(!value)return '—';const date=new Date(String(value));return Number.isNaN(date.valueOf())?String(value):hourFormatter.format(date);}
export function clock(value:unknown):string {if(!value)return '—';const date=new Date(String(value));return Number.isNaN(date.valueOf())?String(value):clockFormatter.format(date);}

// 2.9 秒 / 8.1 分钟: step durations range from milliseconds to the ten-minute reply pause.
export function duration(ms:unknown):string {
 const v=num(ms);
 if(v===null)return '—';
 if(v<1000)return `${Math.round(v)} 毫秒`;
 if(v<90000)return `${(v/1000).toFixed(1)} 秒`;
 return `${(v/60000).toFixed(1)} 分钟`;
}
export function seconds(value:unknown):string {const v=num(value);return v===null?'—':duration(v*1000);}
