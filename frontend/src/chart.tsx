import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

export default function AttendanceChart({ days }: { days: { date: string; count: number }[] }) {
  return <ResponsiveContainer width="100%" height="100%" minWidth={0}>
    <BarChart data={days} margin={{ top: 10, right: 6, left: -18, bottom: 0 }}>
      <CartesianGrid vertical={false} stroke="#e1e4e9" />
      <XAxis dataKey="date" tickFormatter={value => value.slice(5)} tick={{ fontSize: 12 }} />
      <YAxis allowDecimals={false} domain={[0, (maximum: number) => Math.max(1, maximum)]} tick={{ fontSize: 12 }} />
      <Tooltip labelFormatter={value => String(value)} formatter={value => [value, 'Check-ins']} />
      <Bar dataKey="count" fill="#002fa7" maxBarSize={42} />
    </BarChart>
  </ResponsiveContainer>
}
