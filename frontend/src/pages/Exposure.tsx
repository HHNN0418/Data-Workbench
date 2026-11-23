import {useMemo} from 'react';
import {Link} from 'react-router-dom';
import {Alert, Table, Tag} from 'antd';
import {useApi, type Row} from '../api';
import {Chart, Metrics, Panel, QueryState, time} from '../common';
import {n, pct} from './pipelineModel';
import {clock, sydney} from './stageViews';
import './stageViews.css';

// Official hourly NPC engagement next to our own output in the same official hour (GET /api/results/exposure).
export const hourStatus:Record<string,{text:string;color?:string}>={reported:{text:'已报告',color:'green'},no_report:{text:'无报告'},
 not_imported:{text:'未导入',color:'orange'},in_progress:{text:'进行中',color:'processing'}};

function ExposureChart({hours}:{hours:Row[]}) {
 const option=useMemo(()=>{
  // Completed hours only, oldest first; the hour in progress has no official report yet.
  const rows=hours.filter(row=>row.status!=='in_progress').reverse();
  return {tooltip:{trigger:'axis'},legend:{top:0},grid:{left:48,right:48,top:36,bottom:28},
   xAxis:{type:'category',data:rows.map(row=>clock(row.end))},
   yAxis:[{type:'value',name:'官方浏览',minInterval:1},{type:'value',name:'我方条数',minInterval:1}],
   series:[{type:'bar',name:'官方浏览',data:rows.map(row=>({value:row.views,itemStyle:row.outage?{color:'#bb8963'}:undefined}))},
    {type:'line',name:'我方原创',yAxisIndex:1,data:rows.map(row=>row.our_posts)},
    {type:'line',name:'我方回复',yAxisIndex:1,data:rows.map(row=>row.our_replies)}]};
 },[hours]);
 return <Chart option={option} height={300} ariaLabel="每个官方小时的浏览和我方发布"/>;
}

export default function Exposure() {
 // Official reports arrive hourly and the server recomputes every five minutes.
 const query=useApi<Row>('/results/exposure',{},300000);
 return <div className="stack">
  <Alert type="info" showIcon title="我们的帖子有没有被看到" description="官方每小时公布一次 NPC 互动：浏览、点赞、回复、关注和立场转变。官方小时按悉尼时间 (hh-1):47 到 hh:47 计，和我们同一小时发出的原创、回复放在一起看。"/>
  <QueryState query={query}>{data=>{
   const hours:Row[]=data.hours||[],recent=data.summary?.recent||{},all=data.summary?.window||{},span=`最近 ${n(data.summary_hours)} 小时`;
   const item=(label:string,key:string,format=n)=>({label,value:format(all[key]),hint:`${span}：${format(recent[key])}`});
   return <div className="stack">
    {data.outage&&<Alert type="warning" showIcon title="平台故障时段" description={data.outage.note}/>}
    <div className="toolbar"><span className="muted">{n(data.window?.hours)} 个官方小时 · 官方报告已导入到 {time(data.imported_until)}{data.live_until&&<> · 实时活动表到 {time(data.live_until)}</>} · 计算于 {time(data.generated_at)}</span>{data.stale&&<Tag color="processing">更新中</Tag>}</div>
    <Metrics items={[item('官方得分','points'),item('浏览','views'),item('点赞','likes'),item('回复','replies'),item('向我方转变','toward'),
     item('我方原创','our_posts'),item('我方回复','our_replies'),{label:'每条原创带来的浏览',value:all.views_per_original,hint:`${span}：${n(recent.views_per_original)}；不含故障和未报告的小时`}]}/>
    <Panel title="每个官方小时" extra={<Link to="/score">得分与目标</Link>}>
     <ExposureChart hours={hours}/>
     <Table<Row> size="small" rowKey="end" dataSource={hours} pagination={{pageSize:24,showSizeChanger:false}} scroll={{x:1100}}
      columns={[
       {title:'官方小时（悉尼）',dataIndex:'end',render:(value:string,row:Row)=><div>{sydney(value)} 结束<div className="view-cell-sub">从 {clock(row.start)} 起</div></div>},
       {title:'状态',dataIndex:'status',render:(value:string,row:Row)=><><Tag color={hourStatus[value]?.color}>{hourStatus[value]?.text||value}</Tag>{row.outage&&<Tag color="warning">平台故障</Tag>}{row.reports>1&&<Tag>{row.reports} 份报告</Tag>}</>},
       {title:'官方得分',dataIndex:'points',align:'right',render:(value:unknown,row:Row)=>row.reports?n(value):'—'},
       {title:'浏览',dataIndex:'views',align:'right',render:(value:unknown,row:Row)=>row.reports?n(value):'—'},
       {title:'点赞',dataIndex:'likes',align:'right',render:(value:unknown,row:Row)=>row.reports?n(value):'—'},
       {title:'回复',dataIndex:'replies',align:'right',render:(value:unknown,row:Row)=>row.reports?n(value):'—'},
       {title:'转发 · 关注',key:'shares',render:(_:unknown,row:Row)=>row.reports?`${n(row.reposts)} · ${n(row.follows)}`:'—'},
       {title:'立场转变',key:'changes',render:(_:unknown,row:Row)=>row.reports?<span>向我方 {n(row.toward)} · 中立 {n(row.neither)} · 远离 {n(row.away)}</span>:'—'},
       {title:'我方原创',dataIndex:'our_posts',align:'right',render:(value:unknown)=>n(value)},
       {title:'我方回复',dataIndex:'our_replies',align:'right',render:(value:unknown)=>n(value)},
       {title:'原创占比',dataIndex:'post_share',align:'right',render:(value:unknown)=>pct(value)},
       {title:'原创进 Trending',dataIndex:'our_trending',align:'right',render:(value:unknown)=>n(value)},
      ]}/>
    </Panel>
    <ul className="view-notes">{(data.notes||[]).map((text:string)=><li key={text}>{text}</li>)}</ul>
   </div>;
  }}</QueryState>
 </div>;
}
