import axios from 'axios';

const api = axios.create({
  baseURL: '/api',
});

export const getSummary = () => api.get('/summary').then(res => res.data);
export const getIpos = () => api.get('/ipos').then(res => res.data);
export const getIpoByName = (name: string) => api.get(`/ipo?name=${encodeURIComponent(name)}`).then(res => res.data);
export const predictWeighted = (subscription: number) => api.post('/predict-weighted', { subscription }).then(res => res.data);
export const refreshData = () => api.post('/refresh').then(res => res.data);

export default api;
