import {useState} from 'react';
import {Alert, Button} from 'antd';
type Status={isPending:boolean;stage:string;elapsedSeconds:number;receivedBytes:number;cancel:()=>Promise<void>};
export default function AIJobStatus({job,label='AI 任务'}:{job:Status;label?:string}){
 const [error,setError]=useState('');
 if(!job.isPending)return null;
 const seconds=Math.max(0,Math.floor(job.elapsedSeconds)),elapsed=seconds>=60?Math.floor(seconds/60)+' 分 '+seconds%60+' 秒':seconds+' 秒';
 return <Alert type={error?'warning':'info'} showIcon role="status" title={job.stage||label+'正在运行'} description={error||'已等待 '+elapsed+(job.receivedBytes?' · 已接收 '+Math.round(job.receivedBytes/1024)+' KB':'')+'。后台持续等待模型完成，关闭页面后仍可回来查看。'} action={<Button onClick={()=>void job.cancel().catch(failure=>setError(failure instanceof Error?failure.message:'取消请求未确认，请重试'))}>取消{label}</Button>}/>;
}
