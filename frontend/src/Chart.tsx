import {useEffect,useRef} from 'react';
import * as echarts from 'echarts/core';
import {LineChart, BarChart, GraphChart, ScatterChart, TreeChart, HeatmapChart} from 'echarts/charts';
import {GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, AriaComponent, VisualMapComponent} from 'echarts/components';
import {SVGRenderer} from 'echarts/renderers';
import type {EChartsCoreOption} from 'echarts/core';
echarts.use([LineChart,BarChart,GraphChart,ScatterChart,TreeChart,HeatmapChart,GridComponent,TooltipComponent,LegendComponent,DataZoomComponent,AriaComponent,VisualMapComponent,SVGRenderer]);
export interface ChartProps {
 option:EChartsCoreOption;
 height?:number;
 ariaLabel?:string;
 onEvents?:Record<string,(event:any)=>void>;
}
export default function Chart({option,height=280,ariaLabel='数据图表',onEvents}:ChartProps) {
 const ref=useRef<HTMLDivElement>(null); const chart=useRef<echarts.EChartsType|null>(null);
 useEffect(()=>{if(!ref.current)return;chart.current=echarts.init(ref.current,undefined,{renderer:'svg'});const observer=new ResizeObserver(()=>chart.current?.resize());observer.observe(ref.current);return()=>{observer.disconnect();chart.current?.dispose();chart.current=null}},[]);
 useEffect(()=>{chart.current?.setOption({color:['#26705b','#9caa8c','#bb8963','#666c87'],textStyle:{fontFamily:'inherit'},aria:{enabled:true,label:{description:ariaLabel}},...option},true)},[option,ariaLabel]);
 useEffect(()=>{
  const instance=chart.current;
  if(!instance||!onEvents)return;
  const bindings=Object.entries(onEvents);
  bindings.forEach(([name,handler])=>instance.on(name,handler));
  return()=>{bindings.forEach(([name,handler])=>instance.off(name,handler));};
 },[onEvents]);
 return <div ref={ref} style={{height,width:'100%',minWidth:0}} role="img" aria-label={ariaLabel}/>;
}
