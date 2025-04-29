new Vue({
  el: '#app',
  data: {
    strip: null,
    error: ''
  },
  methods: {
    // 呼叫後端 API 取得 HS300 狀態
    fetchState() {
      axios.get('/api/hs300/state', {
        params: { ip: '192.168.50.18' }
      })
      .then(response => {
        this.strip = response.data;
        this.error = '';
      })
      .catch(error => {
        this.error = error.toString();
      });
    },
    // 控制指定插座開或關
    controlPlug(index, action) {
      axios.post('/api/hs300/control', {
        ip: '192.168.50.18',
        socket: index,
        action: action
      })
      .then(response => {
        alert('操作成功，耗電數據：' + JSON.stringify(response.data.emeter));
        this.fetchState();
      })
      .catch(error => {
        alert('操作失敗：' + error);
      });
    }
  }
});
