type ReferenceFailure = {error?:string;error_code?:string};

export function referenceFailureDetails(reference:ReferenceFailure):{message:string;notice?:string} {
 const message=reference.error?.trim()||'历史记录未保存失败原因';
 if(reference.error_code==='output_truncated'&&/提高输出上限或关闭思考模式|可调整输出预算/.test(message)){
  return {message:'当次模型回答因输出长度限制被截断，未获得完整结果。',notice:'历史版本错误；当前已取消本地输出上限。'};
 }
 return {message};
}
