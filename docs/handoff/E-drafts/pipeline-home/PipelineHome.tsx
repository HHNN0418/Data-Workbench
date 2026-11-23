import {Fragment} from 'react';
import {Link} from 'react-router-dom';
import {Alert} from 'antd';
import {useApi, type Row} from '../api';
import {Metrics, QueryState, fmt, time} from '../common';
import {RelativeTime, StatusTag} from '../ui/status';
import PaceAlert from './PaceAlert';
import {bottleneck, flowLabel, nodeView, stageLinks, stageOrder, stageTone, type StageKey} from './pipelineModel';
import './pipeline.css';

const steps=['①','②','③','④','⑤','⑥'];
const statusText:Record<string,string>={ok:'正常',warn:'注意',bad:'异常',unknown:'读取失败'};

// Home: the whole chain from collection to score on one screen (docs/PIPELINE_UI_SPEC.md §3).
export default function PipelineHome() {
 const pipeline=useApi<Row>('/pipeline/status',{},60000,true),overview=useApi<Row>('/overview',{},60000);
 return <div className="stack">
  <PaceAlert threshold={overview.data?.threshold}/>
  <QueryState query={pipeline}>{data=>{
   const stages=stageOrder.map(key=>(data.stages||[]).find((stage:Row)=>stage.key===key)).filter(Boolean) as Row[];
   const byKey=Object.fromEntries(stages.map(stage=>[stage.key,stage])) as Partial<Record<StageKey,Row>>;
   const outcome=byKey.outcome?.metrics||{},worst=bottleneck(stages);
   return <>
    <Metrics items={[
     {label:'官方得分',value:outcome.total,hint:outcome.score_stale?'快照可能过期':undefined},
     {label:'当前速度（6h / 12h）',value:outcome.points_per_hour_6h,hint:`12h ${fmt(outcome.points_per_hour_12h)} 分/时`},
     {label:`Poll ${outcome.next_poll??'—'} 需要`,value:outcome.required_points_per_hour,hint:'分 / 小时'},
     {label:'距门槛还差',value:outcome.gap_to_threshold,hint:outcome.on_track===false?'按当前速度达不到':outcome.on_track?'按当前速度能达到':undefined}
    ]}/>
    {worst&&<Alert type={worst.status==='bad'?'error':'warning'} showIcon title={`瓶颈在「${worst.label}」`} description={worst.headline} action={<Link to={stageLinks[worst.key as StageKey]}>查看{worst.label}</Link>}/>}
    <div className="pipeline-flow" role="list" aria-label="全链路">
     {stages.map((stage,index)=>{
      const key=stage.key as StageKey,view=nodeView(key,stage.metrics),tone=stageTone(stage.status);
      return <Fragment key={key}>
       <Link role="listitem" to={stageLinks[key]} className={`pipeline-node pipeline-${tone}${worst?.key===key?' pipeline-bottleneck':''}`} aria-label={`${steps[index]} ${stage.label}：${statusText[stage.status]||stage.status}`}>
        <div className="pipeline-node-head"><span className="pipeline-step">{steps[index]}</span><strong>{stage.label}</strong><StatusTag tone={tone}>{statusText[stage.status]||stage.status}</StatusTag></div>
        <div className="pipeline-main"><strong>{view.main}</strong><span>{view.mainLabel}</span></div>
        <ul className="pipeline-details">{view.details.map(detail=><li key={detail}>{detail}</li>)}</ul>
        <p className="pipeline-headline">{stage.status==='unknown'?`这一段读取失败${stage.error?`（${stage.error}）`:''}`:stage.headline}</p>
        <span className="pipeline-updated">更新 <RelativeTime value={stage.updated_at}/></span>
       </Link>
       {index<stages.length-1&&<div className="pipeline-arrow" aria-hidden="true"><span>{flowLabel(key,byKey)}</span></div>}
      </Fragment>;
     })}
    </div>
    <Link className="pipeline-loop" to="/results/attribution">⤺ 效果回流到策略：归因已结算 {fmt(outcome.attribution_settled)} / {fmt(outcome.attribution_total)}</Link>
    <p className="muted">链路状态更新于 {time(data.generated_at)}；每段的阈值由后端决定，这里只显示结果。</p>
   </>;
  }}</QueryState>
 </div>;
}
