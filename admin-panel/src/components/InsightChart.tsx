import { useEffect, useRef } from 'react'
import { init, use as registerCharts, type EChartsCoreOption } from 'echarts/core'
import { BarChart, PieChart } from 'echarts/charts'
import { AriaComponent, GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { SVGRenderer } from 'echarts/renderers'

registerCharts([BarChart, PieChart, AriaComponent, GridComponent, LegendComponent, TooltipComponent, SVGRenderer])
export default function InsightChart({ option, label, className = '' }: { option: EChartsCoreOption; label: string; className?: string }) {
  const element = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!element.current) return
    const chart = init(element.current, undefined, { renderer: 'svg' })
    chart.setOption({ ...option, aria: { enabled: true, label: { description: label } } })
    const observer = new ResizeObserver(() => chart.resize())
    observer.observe(element.current)
    return () => { observer.disconnect(); chart.dispose() }
  }, [option, label])
  return <div ref={element} role="img" aria-label={label} className={`insight-chart ${className}`} />
}
