import type {ReactNode} from 'react';
import {Tag} from 'antd';
import {time} from '../common';
import './ui.css';

// The five status meanings used across the site (docs/UX_IA_PLAN.md §13.1); pages never pick raw colours.
export type Tone='ok'|'warn'|'danger'|'info'|'neutral';
const tagColors:Record<Tone,string>={ok:'success',warn:'warning',danger:'error',info:'processing',neutral:'default'};

export function StatusTag({tone,children,title}:{tone:Tone;children:ReactNode;title?:string}) {
 return <Tag color={tagColors[tone]} data-tone={tone} title={title}>{children}</Tag>;
}

const stanceLabels:Record<string,string>={yes:'支持 Yes',no:'反对 No',undecided:'犹豫',unrelated:'无关'};
// Stance colours avoid red and green: the team side can be Yes or No, so neither side is "good".
export function StanceTag({stance,teamSide}:{stance?:string|null;teamSide?:string|null}) {
 const key=stance&&stanceLabels[stance]?stance:'unlabelled';
 const ours=teamSide?/no/i.test(teamSide)?'no':/yes/i.test(teamSide)?'yes':undefined:undefined;
 return <span className={`stance-tag stance-${key}`} data-stance={key}>{stanceLabels[key]||'未标注'}{ours&&stance===ours&&<small className="stance-ours">我方立场</small>}</span>;
}

export function IdentityTags({isOwn,isBot,botReasons}:{isOwn?:unknown;isBot?:unknown;botReasons?:unknown}) {
 const duplicate=!isBot&&Array.isArray(botReasons)&&botReasons.includes('duplicate_text');
 if(!isOwn&&!isBot&&!duplicate)return null;
 return <span className="identity-tags">
  {(isOwn===true||isOwn===1)&&<span className="identity-tag identity-own">本队</span>}
  {isBot===true&&<span className="identity-tag identity-bot">疑似机器人</span>}
  {duplicate&&<span className="identity-tag identity-duplicate" title="和其他账号发过完全相同的长文本">疑似重复文本</span>}
 </span>;
}

// "3 分钟前" with the full Sydney time on hover; for freshness fields such as last collected or last run.
export function RelativeTime({value,now=Date.now()}:{value:unknown;now?:number}) {
 if(!value)return <span className="muted">—</span>;
 const date=typeof value==='number'?new Date(value*1000):new Date(String(value));
 if(Number.isNaN(date.valueOf()))return <span>{String(value)}</span>;
 const minutes=Math.round((now-date.valueOf())/60000),ahead=minutes<0,size=Math.abs(minutes);
 const text=size<1?'刚刚':size<60?`${size} 分钟${ahead?'后':'前'}`:size<24*60?`${Math.round(size/60)} 小时${ahead?'后':'前'}`:time(date.toISOString());
 return <time dateTime={date.toISOString()} title={time(date.toISOString())}>{text}</time>;
}
