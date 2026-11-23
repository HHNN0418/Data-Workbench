import {useMemo, useState} from 'react';
import {Alert, Segmented, Select, Table, Tag} from 'antd';
import {useApi, type Row} from '../api';
import {Chart, Metrics, Panel, QueryState, time} from '../common';
import {NextStep} from '../ui/layout';
import {n, num} from './pipelineModel';
import './leaderboard.css';

// Competition leaderboard (D104, GET /api/leaderboard). The paired extension reads the portal page read-only
// every 10–15 minutes; the server parses it and works out each team's 1h / 6h / 24h change and points per hour.
export const MAX_TEAMS=8;
// Categorical slots in fixed order (validated palette, adjacent pairs); slot 0 is always our team.
export const PALETTE=['#2a78d6','#eb6834','#1baf7a','#eda100','#e87ba4','#008300','#6250d6','#e34948'];
const STALE_MINUTES=30;
const ranges=[{value:24,label:'24 小时'},{value:48,label:'48 小时'},{value:168,label:'7 天'}];
const modes=[{value:'score',label:'分数'},{value:'growth',label:'窗口内增长'}];
type Mode='score'|'growth';

export function signed(value:unknown):string {const v=num(value);return v===null?'—':`${v>0?'+':''}${n(v)}`;}
const tone=(value:unknown)=>{const v=num(value);return v===null||v===0?'':v>0?'board-up':'board-down';};
const axisTime=new Intl.DateTimeFormat('zh-CN',{timeZone:'Australia/Sydney',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});

// The top five plus our own team, shown until the user picks teams.
export function defaultTeams(rows:Row[]):string[] {
 const own=rows.find(row=>row.is_own)?.team;
 return [...new Set([...(own?[own]:[]),...rows.slice(0,5).map(row=>row.team)])].slice(0,MAX_TEAMS);
}

// Colour follows the team: survivors keep their slot when others are added or removed; our team keeps slot 0.
export function assignSlots(previous:Record<string,number>,teams:string[],own?:string):Record<string,number> {
 const slots:Record<string,number>={};
 for(const team of teams)if(team===own)slots[team]=0;else if(previous[team]!==undefined&&previous[team]!==0)slots[team]=previous[team];
 for(const team of teams)if(slots[team]===undefined){
  const used=new Set(Object.values(slots));
  slots[team]=Array.from({length:PALETTE.length},(_,i)=>i).find(i=>i>0&&!used.has(i))??1;
 }
 return slots;
}

export function curveOption(series:Record<string,Row[]>,slots:Record<string,number>,mode:Mode,own?:string) {
 const teams=Object.keys(series).filter(team=>slots[team]!==undefined).sort((a,b)=>slots[a]-slots[b]);
 return {animation:false,tooltip:{trigger:'axis',confine:true,valueFormatter:(value:unknown)=>mode==='growth'?signed(value):n(value)},
  legend:{top:0,type:'scroll'},grid:{left:64,right:24,top:40,bottom:32},
  xAxis:{type:'time',axisLabel:{fontSize:11,hideOverlap:true,formatter:(value:number)=>axisTime.format(new Date(value))},splitLine:{show:false}},
  yAxis:{type:'value',scale:true,name:mode==='growth'?'窗口内增长':'分数',splitLine:{lineStyle:{color:'#eef1eb'}}},
  series:teams.map(team=>{
   const points=series[team]||[],first=num(points[0]?.score)??0,color=PALETTE[slots[team]%PALETTE.length];
   return {name:team===own?`${team}（我方）`:team,type:'line',showSymbol:false,symbolSize:8,connectNulls:false,
    lineStyle:{width:2,color},itemStyle:{color},data:points.map(point=>[point.at,mode==='growth'?(num(point.score)??0)-first:num(point.score)])};
  })};
}

function Freshness({data}:{data:Row}) {
 const observed=data.snapshot?.observed_at,age=observed?(Date.now()-Date.parse(observed))/60000:null,failure=data.last_failure;
 return <>
  {age!==null&&age>STALE_MINUTES&&<Alert type="warning" showIcon title={`排行榜已经 ${n(Math.round(age))} 分钟没有更新`}
   description="扩展每 10–15 分钟读一次。打开 Chrome 里「CTN 本队积分同步」的弹窗，看「排行榜」那一行；版本要是 1.4.0（在 chrome://extensions 里点这个扩展的刷新按钮）。门户退出登录时，扩展会暂停读取，登录后自动恢复。"/>}
  {failure&&(!observed||String(failure.observed_at)>String(observed))&&<Alert type="error" showIcon title="最近一次排行榜表格没能解析" description={`${time(failure.observed_at)}：${failure.error}。页面结构可能变了，需要改 ctn_data/leaderboard.py；样本已存在 meta.leaderboard_parse_failure。`}/>}
 </>;
}

function OwnMetrics({own,snapshot}:{own:Row;snapshot:Row}) {
 const window=own.speed_window?`最近 ${n(own.speed_window)} 小时`:'历史还不够 1 小时';
 const gap=(other:Row|null|undefined,label:string)=>other?{label,value:other.gap,hint:`${other.team}（第 ${n(other.rank)} 名，${n(other.speed_per_hour)} 分/时）${other.catch_up_hours!=null?` · 按现在的速度约 ${n(other.catch_up_hours)} 小时追上`:' · 按现在的速度追不上'}`}:{label,value:null,hint:'我们就是这个位置'};
 return <Metrics items={[
  {label:'我方名次',value:own.rank,hint:`共 ${n(snapshot.teams)} 队，${n(snapshot.scored_teams)} 队有分${own.rank_change_24h?` · 24 小时 ${signed(own.rank_change_24h)} 名`:''}`},
  {label:'我方分数',value:own.score,hint:`1 小时 ${signed(own.change_1h)} · 6 小时 ${signed(own.change_6h)} · 24 小时 ${signed(own.change_24h)}`},
  {label:'每小时速度',value:own.speed_per_hour,hint:window},
  gap(own.above,'与上一名的差距'),gap(own.first,'与第 1 名的差距')]}/>;
}

function Ranking({rows,selected,onSelect}:{rows:Row[];selected:string[];onSelect:(teams:string[])=>void}) {
 const change=(key:string,title:string)=>({title,dataIndex:key,align:'right' as const,sorter:(a:Row,b:Row)=>(num(a[key])??-Infinity)-(num(b[key])??-Infinity),
  render:(value:unknown)=><span className={tone(value)}>{signed(value)}</span>});
 return <Table<Row> size="small" rowKey="team" dataSource={rows} scroll={{x:900}} pagination={rows.length>50?{pageSize:50,showSizeChanger:false}:false}
  rowClassName={row=>row.is_own?'board-own':''}
  rowSelection={{selectedRowKeys:selected,hideSelectAll:true,columnTitle:'曲线',
   getCheckboxProps:row=>({disabled:!selected.includes(row.team)&&selected.length>=MAX_TEAMS,'aria-label':`在曲线里显示 ${row.team}`}),
   onChange:keys=>onSelect(rows.filter(row=>keys.includes(row.team)).map(row=>row.team))}}
  columns={[
   {title:'名次',dataIndex:'rank',width:90,sorter:(a,b)=>(num(a.rank)??1e9)-(num(b.rank)??1e9),
    render:(value:unknown,row:Row)=><>{n(value)}{num(row.rank_change_24h)?<span className={`board-rank ${tone(row.rank_change_24h)}`} title="24 小时名次变化">{num(row.rank_change_24h)!>0?'↑':'↓'}{Math.abs(num(row.rank_change_24h)!)}</span>:null}</>},
   {title:'队伍',dataIndex:'team',render:(value:string,row:Row)=><span className="board-team">{value}{row.is_own&&<Tag color="green">我方</Tag>}</span>},
   {title:'分数',dataIndex:'score',align:'right',sorter:(a,b)=>(num(a.score)??0)-(num(b.score)??0),render:(value:unknown)=>n(value)},
   change('change_1h','1 小时'),change('change_6h','6 小时'),change('change_24h','24 小时'),
   {title:<span title="变化 ÷ 两次快照的实际间隔；优先用 6 小时窗口，没有时用 24 小时、再用 1 小时">每小时速度</span>,dataIndex:'speed_per_hour',align:'right',
    sorter:(a,b)=>(num(a.speed_per_hour)??-Infinity)-(num(b.speed_per_hour)??-Infinity),
    render:(value:unknown,row:Row)=><>{n(value)}{row.speed_window&&<span className="muted"> · {n(row.speed_window)}h</span>}</>}
  ]}/>;
}

function Board({data,selected,setSelected,hours,setHours}:{data:Row;selected:string[];setSelected:(teams:string[])=>void;hours:number;setHours:(hours:number)=>void}) {
 const [mode,setMode]=useState<Mode>('score');
 const [slots,setSlots]=useState<Record<string,number>>({});
 const rows:Row[]=data.teams||[],own=rows.find(row=>row.is_own)?.team;
 const current=useMemo(()=>assignSlots(slots,selected,own),[slots,selected,own]);
 const choose=(teams:string[])=>{const next=teams.slice(0,MAX_TEAMS);setSlots(assignSlots(current,next,own));setSelected(next);};
 const option=useMemo(()=>curveOption(data.series||{},current,mode,own),[data.series,current,mode,own]);
 const snapshot=data.snapshot||{};
 return <div className="stack">
  <Freshness data={data}/>
  <div className="toolbar"><span className="muted">最新快照 {time(snapshot.observed_at)} · 最近 24 小时 {n(snapshot.snapshots_24h)} 次 · 从 {time(snapshot.first_observed_at)} 开始记录 · 计算于 {time(data.generated_at)}</span>{data.stale&&<Tag color="processing">更新中</Tag>}</div>
  {data.own?<OwnMetrics own={data.own} snapshot={snapshot}/>:<Alert type="info" showIcon title={`排行榜里没找到「${data.own_team||'Agent Zero'}」`} description="按队名和本队页面的队伍 ID 匹配；队名改过时，等扩展下一次读取后会按队伍 ID 认出来。"/>}
  <Panel title="分数曲线" extra={<div className="toolbar">
   <Segmented size="small" value={hours} options={ranges} onChange={value=>setHours(Number(value))}/>
   <Segmented size="small" value={mode} options={modes} onChange={value=>setMode(value as Mode)}/>
  </div>}>
   <Select mode="multiple" allowClear showSearch className="board-picker" aria-label="选择曲线里的队伍" placeholder={`选择队伍（最多 ${MAX_TEAMS} 支）`}
    value={selected} maxCount={MAX_TEAMS} onChange={choose} options={rows.map(row=>({value:row.team,label:`${n(row.rank)}. ${row.team}${row.is_own?'（我方）':''}`}))}/>
   {selected.length?<Chart option={option} height={340} ariaLabel={`${selected.length} 支队伍的分数曲线`}/>:<p className="muted">在上面或表格左侧选几支队伍。</p>}
   <p className="muted board-note">「窗口内增长」把每条曲线的起点都放到 0，分数差很多的队伍也能比较涨得快慢。</p>
  </Panel>
  <Panel title="当前排名" extra={<span className="muted">勾选左侧方框把队伍加进曲线</span>}>
   <Ranking rows={rows} selected={selected} onSelect={choose}/>
  </Panel>
  <ul className="view-notes">
   <li>变化 = 最新分数 − N 小时前那次快照的分数；那次快照要在 N 到 1.5N 小时之前，否则显示 —。</li>
   <li>每小时速度 = 变化 ÷ 两次快照的实际间隔；优先 6 小时窗口。</li>
   <li>数据只读：扩展在已登录的本队页面里每 10–15 分钟 GET 一次 /user/competitions/2，不点任何东西。</li>
  </ul>
 </div>;
}

export default function Leaderboard() {
 const [picked,setPicked]=useState<string[]|null>(null);
 const [hours,setHours]=useState(48);
 const [seed,setSeed]=useState<string[]>([]);
 const selected=picked??seed;
 // The server caches the ranking for a minute; snapshots arrive every 10–15 minutes.
 const query=useApi<Row>('/leaderboard',{teams:selected.join('\n'),hours},120000,true);
 const rows:Row[]=query.data?.teams||[];
 if(picked===null&&!seed.length&&rows.length)setSeed(defaultTeams(rows));
 return <div className="stack">
  <Alert type="info" showIcon title="所有队伍的得分和变化" description="来自竞赛门户的排行榜（只读）。扩展每 10–15 分钟读一次，后端记下每次快照，算出每队 1、6、24 小时的分数变化和每小时速度。"/>
  <QueryState query={query}>{data=>data.status!=='ok'
   ?<><Freshness data={data}/><NextStep title="还没有排行榜快照" description="上线后在 chrome://extensions 里点「CTN 本队积分同步」的刷新按钮，让 1.4.0 生效；保持本队页面标签和门户登录，10–15 分钟内会有第一份快照。"/></>
   :<Board data={data} selected={selected} setSelected={setPicked} hours={hours} setHours={setHours}/>}</QueryState>
 </div>;
}
