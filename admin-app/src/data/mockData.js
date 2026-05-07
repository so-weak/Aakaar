export const CREDENTIALS = {
  userId: 'K22408m',
  password: 'hdfc@123'
}

export const DASHBOARD_METRICS = {
  pending: 0,
  preApproved: 0,
  approved: 116,
  rejected: 9
}

export const LAST_LOGIN = '07 May 2026 & 10:58:05'

export const SWITCH_TYPES = [
  'NFS',
  'VISA',
  'MasterCard',
  'RuPay',
  'IMPS',
  'UPI'
]

export const CYCLE_NUMBERS = ['C01', 'C02', 'C03', 'C04']

export const RECON_UPLOAD_HISTORY = [
  {
    id: 1,
    fileName: 'NFS_C02_06052026.csv',
    switchType: 'NFS',
    cycle: 'C02',
    date: '06/05/2026',
    status: 'Uploaded',
    uploadedAt: '07/05/2026 10:42:11'
  },
  {
    id: 2,
    fileName: 'VISA_C01_05052026.zip',
    switchType: 'VISA',
    cycle: 'C01',
    date: '05/05/2026',
    status: 'Processed',
    uploadedAt: '06/05/2026 18:11:02'
  },
  {
    id: 3,
    fileName: 'RUPAY_C03_04052026.csv',
    switchType: 'RuPay',
    cycle: 'C03',
    date: '04/05/2026',
    status: 'Failed',
    uploadedAt: '05/05/2026 09:30:55'
  }
]
